import unittest
import tempfile
from pathlib import Path
from receivables.service import Workbench
from receivables.primitives import BookError
from receivables.demonstration import run


class ClosingCase(unittest.TestCase):
    def setUp(self):
        self.w = Workbench()
        self.addCleanup(self.w.close)
        self.w.ledger.register_party('a', 'Alpha', ['A Ltd'])
        self.w.ledger.post(dict(id='i', kind='invoice', party='a', currency='CNY', amount='100',
                               issued='2026-01-01', due='2026-01-31', reference='R'))
        self.w.ledger.post(dict(id='p', kind='payment', party='a', currency='CNY', amount='60',
                               issued='2026-01-10', reference='R'))

    def test_half_open_period_boundaries_and_overlap(self):
        self.w.periods.create('jan', '2026-01-01', '2026-02-01')
        self.w.periods.create('feb', '2026-02-01', '2026-03-01')
        with self.assertRaises(BookError):
            self.w.periods.create('overlap', '2026-01-31', '2026-03-01')
        with self.assertRaises(BookError):
            self.w.periods.create('empty', '2026-01-01', '2026-01-01')
        self.assertEqual(self.w.periods.get('jan')['state'], 'open')
        self.assertEqual(self.w.periods.create('jan', '2026-01-01', '2026-02-01')['state'], 'open')

    def test_closed_period_rejects_backdated_writes(self):
        self.w.periods.create('jan', '2026-01-01', '2026-02-01')
        preview = self.w.periods.preview('jan')
        self.w.periods.close('jan', preview['digest'], 'controller')
        with self.assertRaises(BookError) as caught:
            self.w.ledger.allocate('x', 'p', 'i', '10', '2026-01-31')
        self.assertEqual(caught.exception.code, 'period')
        self.w.ledger.allocate('x', 'p', 'i', '10', '2026-02-01')
        self.assertTrue(self.w.periods.verify('jan')['verified'])
        self.assertEqual(self.w.ledger.balance('i'), 9000)

    def test_stale_close_digest_requires_new_preview(self):
        self.w.periods.create('jan', '2026-01-01', '2026-02-01')
        preview = self.w.periods.preview('jan')
        self.w.ledger.allocate('x', 'p', 'i', '10', '2026-01-20')
        with self.assertRaises(BookError):
            self.w.periods.close('jan', preview['digest'], 'controller')
        self.assertEqual(self.w.periods.get('jan')['state'], 'open')
        fresh = self.w.periods.preview('jan')
        self.assertNotEqual(fresh['digest'], preview['digest'])
        self.w.periods.close('jan', fresh['digest'], 'controller')
        self.assertTrue(self.w.periods.verify('jan')['verified'])

    def test_future_dispute_resolution_preserves_closed_evidence(self):
        self.w.disputes.open('d', 'i', '20', '2026-01-15', 'quality')
        self.w.periods.create('jan', '2026-01-01', '2026-02-01')
        preview = self.w.periods.preview('jan')
        self.w.periods.close('jan', preview['digest'], 'controller')
        self.w.disputes.close('d', '2026-02-01', 1, 'resolved')
        self.assertTrue(self.w.periods.verify('jan')['verified'])
        self.assertEqual(len(self.w.disputes.list('2026-01-31')), 1)
        self.assertEqual(self.w.disputes.list('2026-02-01'), [])
        self.assertEqual(self.w.disputes.get('d')['revision'], 2)

    def test_disputes_limit_overlapping_principal(self):
        self.w.disputes.open('d1', 'i', '70', '2026-01-15', 'quality')
        with self.assertRaises(BookError):
            self.w.disputes.open('d2', 'i', '31', '2026-01-16', 'delivery')
        self.w.disputes.open('d2', 'i', '30', '2026-01-16', 'delivery')
        self.assertEqual(len(self.w.disputes.list('2026-01-20')), 2)
        self.w.disputes.close('d1', '2026-02-01', 1, 'resolved')
        self.w.disputes.open('d3', 'i', '70', '2026-02-01', 'new')
        self.assertEqual(len(self.w.disputes.list('2026-02-01')), 2)
        self.assertTrue(self.w.controls.require_valid()['ok'])

    def test_dispute_optimistic_revision_and_input_errors(self):
        self.w.disputes.open('d', 'i', '20', '2026-01-15', 'quality')
        with self.assertRaises(BookError):
            self.w.disputes.close('d', '2026-02-01', 2, 'resolved')
        with self.assertRaises(BookError):
            self.w.disputes.close('d', '2026-01-14', 1, 'resolved')
        with self.assertRaises(BookError):
            self.w.disputes.open('p-dispute', 'p', '1', '2026-01-15', 'invalid')
        self.assertIsNone(self.w.disputes.get('d')['closed'])
        self.assertEqual(self.w.disputes.get('d')['revision'], 1)

    def test_aging_separates_disputed_and_collectible(self):
        self.w.ledger.allocate('x', 'p', 'i', '60', '2026-02-01')
        self.w.disputes.open('d', 'i', '30', '2026-02-02', 'quality')
        report = self.w.aging.report('2026-02-28', 'a')
        group = report['groups'][0]
        self.assertEqual(group['open_minor'], 4000)
        self.assertEqual(group['disputed_minor'], 3000)
        self.assertEqual(group['collectible_minor'], 1000)
        self.assertEqual(sum(r['open_minor'] for r in report['invoices']), 4000)
        self.assertEqual(self.w.aging.report('2025-12-31')['invoices'], [])

    def test_aging_caps_disputes_after_payment(self):
        self.w.disputes.open('d', 'i', '100', '2026-01-15', 'quality')
        self.w.ledger.allocate('x', 'p', 'i', '60', '2026-02-01')
        report = self.w.aging.report('2026-02-02')
        self.assertEqual(report['groups'][0]['disputed_minor'], 4000)
        self.assertEqual(report['groups'][0]['collectible_minor'], 0)
        self.assertEqual(self.w.queue.build('2026-02-02')['rows'], [])
        self.assertEqual(self.w.ledger.balance('i'), 4000)
        self.assertTrue(self.w.controls.require_valid()['ok'])

    def test_currency_groups_are_never_net_combined(self):
        self.w.ledger.post(dict(id='u', kind='invoice', party='a', currency='USD', amount='20',
                               issued='2026-01-01', due='2026-01-31', reference='USD'))
        groups = self.w.queue.exposure('a', '2026-02-01')['currencies']
        self.assertEqual([r['currency'] for r in groups], ['CNY', 'USD'])
        self.assertEqual(groups[0]['net_exposure_minor'], 4000)
        self.assertEqual(groups[1]['net_exposure_minor'], 2000)
        balances = self.w.controls.trial_balance('2026-02-01')['currencies']
        self.assertTrue(all(row['difference_minor'] == 0 for row in balances))
        self.assertEqual(len(balances), 2)

    def test_statement_is_frozen_and_reports_late_change(self):
        first = self.w.statements.freeze('s', 'a', '2026-02-01')
        self.assertTrue(self.w.statements.compare('s')['unchanged'])
        self.w.ledger.allocate('x', 'p', 'i', '60', '2026-02-01')
        self.assertEqual(self.w.statements.get('s'), first)
        changed = self.w.statements.compare('s')
        self.assertFalse(changed['unchanged'])
        self.assertTrue(changed['unapplied_changed'])
        self.assertEqual(changed['invoice_changes'][0]['after']['open_minor'], 4000)
        self.assertEqual(first['aging']['groups'][0]['open_minor'], 10000)

    def test_export_roundtrip_tamper_and_no_overwrite(self):
        self.w.statements.freeze('s', 'a', '2026-02-01')
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'out.json'
            result = self.w.statements.export('s', target)
            self.assertGreater(result['bytes'], 0)
            self.assertTrue(self.w.statements.verify(target)['verified'])
            with self.assertRaises(BookError):
                self.w.statements.export('s', target)
            target.write_text(target.read_text().replace('Alpha', 'Altered'))
            with self.assertRaises(BookError):
                self.w.statements.verify(target)

    def test_csv_prevents_formula_reference_execution(self):
        self.w.ledger.post(dict(id='formula', kind='invoice', party='a', currency='CNY', amount='10',
                               issued='2026-01-01', due='2026-01-31', reference='=1+2'))
        self.w.statements.freeze('s', 'a', '2026-02-01')
        output = self.w.statements.csv('s')
        self.assertIn("'=1+2", output)
        self.assertIn('invoice,currency,issued,due,reference,open,bucket', output)
        self.assertEqual(len(output.splitlines()), 3)
        self.assertTrue(self.w.statements.compare('s')['unchanged'])

    def test_queue_due_date_forecast_is_explicit(self):
        report = self.w.queue.forecast('2026-01-15', 30)
        self.assertEqual(report['basis'], 'contractual_due_dates_not_payment_prediction')
        self.assertEqual(report['rows'][0]['date'], '2026-01-31')
        self.assertEqual(report['rows'][0]['collectible_minor'], 10000)
        self.assertEqual(self.w.queue.build('2026-01-30')['eligible'], 0)
        self.assertEqual(self.w.queue.build('2026-02-01')['eligible'], 1)
        self.assertEqual(self.w.queue.build('2026-02-01')['rows'][0]['days_overdue'], 1)

    def test_bank_link_reconcile_and_unlink(self):
        self.w.bank.add(dict(id='b', account='cash', currency='CNY', amount='60',
                             booked='2026-01-11', reference='R', payer='A Ltd'))
        candidates = self.w.bank.candidates('b')
        self.assertTrue(candidates)
        self.w.bank.link('b', 'p', 'operator')
        self.assertEqual(self.w.db.one('SELECT payment FROM bank_links WHERE line=?', ('b',))['payment'], 'p')
        with self.assertRaises(BookError):
            self.w.ledger.reverse_document('r', 'p', '2026-02-01', 'void')
        self.w.bank.unlink('b', 'wrong evidence')
        self.w.ledger.reverse_document('r', 'p', '2026-02-01', 'void')
        self.assertEqual(self.w.ledger.balance('p'), 0)

    def test_bank_mismatched_amount_cannot_link(self):
        self.w.bank.add(dict(id='b', account='cash', currency='CNY', amount='61',
                             booked='2026-01-11', reference='R', payer='Alpha'))
        with self.assertRaises(BookError):
            self.w.bank.link('b', 'p', 'operator')
        self.assertEqual(self.w.db.rows('SELECT * FROM bank_links'), [])
        self.assertEqual(self.w.ledger.balance('p'), 6000)
        self.assertTrue(self.w.controls.require_valid()['ok'])
        with self.assertRaises(BookError):
            self.w.bank.link('b', 'i', 'operator')

    def test_journal_interval_and_party_filter(self):
        self.w.ledger.allocate('x', 'p', 'i', '10', '2026-02-01')
        rows = self.w.controls.journal('2026-01-01', '2026-02-01', 'a')
        self.assertEqual(len(rows), 4)
        self.assertEqual(sum(int(round(float(r['amount']) * 100)) for r in rows), 0)
        self.assertEqual(self.w.controls.journal('2026-02-02', '2026-03-01'), [])
        with self.assertRaises(BookError):
            self.w.controls.journal('2026-02-01', '2026-01-01')
        with self.assertRaises(BookError):
            self.w.controls.journal('2026-01-01', '2026-02-01', 'missing')

    def test_acceptance_uses_real_business_flow(self):
        result = run()
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['historical_open_minor'], 5050)
        # The second reopened-period correction restores another 29.50 of
        # receivable on top of the original 171.00 closing exposure.
        self.assertEqual(result['current_open_minor'], 20050)
        self.assertTrue(result['closed_period_verified'])
        self.assertEqual(result['bridge_opening_minor'], 5050)
        self.assertEqual(result['bridge_closing_minor'], 17100)
        self.assertEqual(len(result['bridge_versions']), 2)
        self.assertTrue(result['offline'])
