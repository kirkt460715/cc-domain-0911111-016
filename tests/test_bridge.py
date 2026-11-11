import unittest
import tempfile
from pathlib import Path
from receivables.service import Workbench
from receivables.primitives import BookError


class BridgeCase(unittest.TestCase):
    def setUp(self):
        self.w = Workbench()
        self.addCleanup(self.w.close)
        self.w.ledger.register_party("a", "Alpha", ["A Ltd"])
        self.w.ledger.register_party("b", "Beta")

    def invoice(self, ident, party="a", amount="100", issued="2026-01-05",
                due="2026-01-31", currency="CNY", reference="R"):
        self.w.ledger.post(
            dict(id=ident, kind="invoice", party=party, currency=currency,
                 amount=amount, issued=issued, due=due, reference=reference)
        )

    def payment(self, ident, amount, party="a", currency="CNY", issued="2026-01-10"):
        self.w.ledger.post(
            dict(id=ident, kind="payment", party=party, currency=currency,
                 amount=amount, issued=issued, reference="R")
        )

    def credit(self, ident, amount, party="a", issued="2026-02-12"):
        self.w.ledger.post(
            dict(id=ident, kind="credit", party=party, currency="CNY",
                 amount=amount, issued=issued, reference="CM")
        )

    def categories(self, report):
        return report["principal_bridge"]["categories_minor"]

    def line(self, report, line_id):
        for row in report["lines"] + report["dispute_lines"]:
            if row["line_id"] == line_id:
                return row
        self.fail("line not found: " + line_id)

    # ------------------------------------------------------------- conservation
    def test_component_totals_strictly_equal_closing_minus_opening(self):
        self.invoice("i1", amount="100")
        self.invoice("i2", amount="50", issued="2026-02-03", due="2026-02-20")
        self.payment("p1", "60")
        self.credit("c1", "10")
        self.w.ledger.allocate("al1", "p1", "i1", "60", "2026-02-11")
        self.w.ledger.allocate("al2", "c1", "i1", "10", "2026-02-13")
        report = self.w.bridges.build("br", ["a"], "CNY", "2026-02-01", "2026-03-01")
        self.assertEqual(report["opening_minor"], 10000)
        self.assertEqual(report["closing_minor"], 8000)
        self.assertEqual(
            report["opening_minor"]
            + sum(self.categories(report).values()),
            report["closing_minor"],
        )
        self.assertEqual(self.categories(report)["new_confirmation"], 5000)
        self.assertEqual(self.categories(report)["cash_collection"], -6000)
        self.assertEqual(self.categories(report)["credit_memo"], -1000)
        self.assertTrue(report["checks"]["passed"])

    def test_aging_migration_only_moves_buckets_and_never_adds_exposure(self):
        # New invoice migrates not_due -> 1_30 inside the window.
        self.invoice("i1", amount="50", issued="2026-02-03", due="2026-02-20")
        report = self.w.bridges.build("br", ["a"], "CNY", "2026-02-01", "2026-03-01")
        flows = report["principal_bridge"]["aging_flows_minor"]
        self.assertEqual(flows["not_due"]["out_minor"], 5000)
        self.assertEqual(flows["1_30"]["in_minor"], 5000)
        self.assertEqual(self.categories(report)["aging_migration"], 0)
        # Each migration line is zero-net and traces to the invoice due date.
        migration = [row for row in report["lines"]
                     if row["category"] == "aging_migration"]
        self.assertTrue(migration)
        for row in migration:
            self.assertEqual(row["amount_minor"], 0)
            self.assertEqual(row["moved_minor"], 5000)
            self.assertEqual(row["evidence"]["basis"], "due_date_aging")
        names = {check["name"]: check["passed"] for check in report["checks"]["checks"]}
        self.assertTrue(names["aging_zero_net"])
        self.assertTrue(names["aging_bucket_conservation"])

    def test_bucket_chain_when_invoice_crosses_several_buckets(self):
        # Due 2026-01-05; at 2026-02-01 late=27 (1_30), at 2026-04-30 late=115 (over_90).
        self.invoice("i1", amount="100", due="2026-01-05")
        report = self.w.bridges.build("br", ["a"], "CNY", "2026-02-01", "2026-05-01")
        walk = report["invoice_walks"][0]
        self.assertEqual(walk["opening_bucket"], "1_30")
        self.assertEqual(walk["closing_bucket"], "over_90")
        migrations = [row for row in report["lines"]
                      if row["category"] == "aging_migration"]
        self.assertEqual(
            [(row["from_bucket"], row["to_bucket"]) for row in migrations],
            [("1_30", "31_60"), ("31_60", "61_90"), ("61_90", "over_90")],
        )
        # Standing balance never duplicated across the chain.
        self.assertEqual(sum(row["moved_minor"] for row in migrations), 30000)
        self.assertTrue(report["checks"]["passed"])

    def test_party_and_bucket_slices_stay_conserved(self):
        self.invoice("i1", party="a", amount="40", due="2026-01-05")
        self.invoice("i2", party="b", amount="70", issued="2026-02-02", due="2026-02-15")
        self.payment("p1", "30", party="b")
        self.w.ledger.allocate("al1", "p1", "i2", "30", "2026-02-09")
        report = self.w.bridges.build("br", ["a", "b"], "CNY", "2026-02-01", "2026-03-01")
        by_party = report["principal_bridge"]["by_party_minor"]
        # Party "a" has no exposure-changing event, so it is absent from the
        # movement map while remaining fully represented in invoice_walks.
        self.assertNotIn("a", by_party)
        self.assertEqual(by_party["b"]["new_confirmation"], 7000)
        self.assertEqual(by_party["b"]["cash_collection"], -3000)
        names = {check["name"]: check["passed"] for check in report["checks"]["checks"]}
        self.assertTrue(names["party_slice_conservation"])
        self.assertTrue(names["aging_bucket_conservation"])

    # ------------------------------------------------------------- categories
    def test_credit_note_is_never_classified_as_cash(self):
        self.invoice("i1")
        self.credit("c1", "25")
        self.w.ledger.allocate("al1", "c1", "i1", "25", "2026-02-13")
        report = self.w.bridges.build("br", ["a"], "CNY", "2026-02-01", "2026-03-01")
        line = self.line(report, "i1:credit_memo:al1")
        self.assertEqual(line["amount_minor"], -2500)
        self.assertFalse(line["cash"])
        cash_lines = [row for row in report["lines"] if row["cash"]]
        self.assertEqual(cash_lines, [])
        self.assertEqual(self.categories(report)["cash_collection"], 0)
        self.assertEqual(self.categories(report)["credit_memo"], -2500)

    def test_write_off_and_reversal_are_gross_separate_events(self):
        self.invoice("i1")
        self.payment("p1", "100")
        self.w.ledger.allocate("al1", "p1", "i1", "100", "2026-01-20")
        self.w.ledger.reverse_allocation("rv1", "al1", "2026-02-05", "re-open")
        self.w.ledger.reverse_document("rd1", "i1", "2026-02-06", "void")
        report = self.w.bridges.build("br", ["a"], "CNY", "2026-02-01", "2026-03-01")
        self.assertEqual(self.categories(report)["write_off_reversal"], 10000)
        self.assertEqual(self.categories(report)["write_off"], -10000)
        self.assertEqual(report["opening_minor"], 0)
        self.assertEqual(report["closing_minor"], 0)
        self.assertTrue(report["checks"]["passed"])

    def test_allocation_created_and_reversed_inside_window_nets_to_zero(self):
        self.invoice("i1")
        self.payment("p1", "40")
        self.w.ledger.allocate("al1", "p1", "i1", "40", "2026-02-11")
        self.w.ledger.reverse_allocation("rv1", "al1", "2026-02-20", "correction")
        report = self.w.bridges.build("br", ["a"], "CNY", "2026-02-01", "2026-03-01")
        self.assertEqual(self.categories(report)["cash_collection"], -4000)
        self.assertEqual(self.categories(report)["write_off_reversal"], 4000)
        self.assertEqual(report["closing_minor"], 10000)

    def test_split_payment_allocates_each_share_once_and_traceably(self):
        self.invoice("i1", amount="60", due="2026-02-05")
        self.invoice("i2", amount="40", due="2026-02-10")
        self.payment("p1", "100")
        self.w.ledger.allocate("al1", "p1", "i1", "60", "2026-02-11")
        self.w.ledger.allocate("al2", "p1", "i2", "40", "2026-02-11")
        report = self.w.bridges.build("br", ["a"], "CNY", "2026-02-01", "2026-03-01")
        ids = {row["line_id"] for row in report["lines"]}
        self.assertIn("i1:cash_collection:al1", ids)
        self.assertIn("i2:cash_collection:al2", ids)
        self.assertEqual(self.categories(report)["cash_collection"], -10000)
        names = {check["name"]: check["passed"] for check in report["checks"]["checks"]}
        self.assertTrue(names["lines_counted_once"])

    # ------------------------------------------------------------- disputes
    def test_dispute_change_is_non_cash_and_reconciles_the_slice(self):
        self.invoice("i1")
        self.w.disputes.open("d1", "i1", "30", "2026-02-05", "quality")
        report = self.w.bridges.build("br", ["a"], "CNY", "2026-02-01", "2026-03-01")
        self.assertEqual(report["opening_disputed_minor"], 0)
        self.assertEqual(report["closing_disputed_minor"], 3000)
        self.assertEqual(report["dispute_bridge"]["change_minor"], 3000)
        self.assertEqual(report["dispute_bridge"]["cash_included_minor"], 0)
        self.assertTrue(all(not row["cash"] for row in report["dispute_lines"]))
        # Gross exposure is untouched by the dispute layer.
        self.assertEqual(report["opening_minor"], report["closing_minor"])

    def test_dispute_is_capped_by_principal_collections_without_cash_label(self):
        self.invoice("i1")
        self.payment("p1", "60")
        self.w.disputes.open("d1", "i1", "100", "2026-02-06", "quality")
        self.w.ledger.allocate("al1", "p1", "i1", "60", "2026-02-07")
        report = self.w.bridges.build("br", ["a"], "CNY", "2026-02-01", "2026-03-01")
        self.assertEqual(report["closing_disputed_minor"], 4000)
        kinds = {row["event_kind"]: row["amount_minor"]
                 for row in report["dispute_lines"]}
        self.assertEqual(kinds["dispute_open"], 10000)
        self.assertEqual(kinds["dispute_reassessment"], -6000)
        self.assertNotIn("cash", [row["event_kind"] for row in report["dispute_lines"]])
        self.assertTrue(report["checks"]["passed"])

    def test_dispute_opened_and_closed_in_window_keeps_both_events(self):
        self.invoice("i1")
        self.w.disputes.open("d1", "i1", "30", "2026-02-05", "q")
        self.w.disputes.close("d1", "2026-02-20", 1, "resolved")
        report = self.w.bridges.build("br", ["a"], "CNY", "2026-02-01", "2026-03-01")
        events = [(row["event_kind"], row["amount_minor"])
                  for row in report["dispute_lines"]]
        self.assertEqual(events, [("dispute_open", 3000), ("dispute_close", -3000)])
        self.assertEqual(report["dispute_bridge"]["change_minor"], 0)
        # Unique line identifiers even though one dispute drives two lines.
        ids = [row["line_id"] for row in report["dispute_lines"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_dispute_reclassification_survives_in_window_write_off(self):
        self.invoice("i1")
        self.payment("p1", "100")
        self.w.ledger.allocate("al1", "p1", "i1", "100", "2026-01-20")
        self.w.disputes.open("d1", "i1", "100", "2026-01-20", "quality")
        self.w.ledger.reverse_allocation("rv1", "al1", "2026-02-05", "re-open")
        self.w.ledger.reverse_document("rd1", "i1", "2026-02-06", "void")
        report = self.w.bridges.build("br", ["a"], "CNY", "2026-02-01", "2026-03-01")
        walk = next(row for row in report["invoice_walks"] if row["invoice"] == "i1")
        self.assertIn("i1", [row["invoice"] for row in report["invoice_walks"]])
        self.assertEqual(walk["disputed_opening_minor"], 0)
        self.assertEqual(walk["disputed_closing_minor"], 0)

    # ------------------------------------------------------------- scope & currency
    def test_scope_is_validated_and_currencies_never_mix(self):
        self.invoice("i1", currency="CNY")
        self.invoice("u1", party="a", currency="USD", amount="20", reference="U")
        with self.assertRaises(BookError) as caught:
            self.w.bridges.build("br", ["a"], "GBP", "2026-02-01", "2026-03-01")
        self.assertEqual(caught.exception.code, "currency")
        with self.assertRaises(BookError):
            self.w.bridges.build("br", ["a"], "CNY", "2026-03-01", "2026-02-01")
        with self.assertRaises(BookError):
            self.w.bridges.build("br", ["ghost"], "CNY", "2026-02-01", "2026-03-01")
        report = self.w.bridges.build("br", ["a"], "USD", "2026-02-01", "2026-03-01")
        self.assertEqual(report["scope"]["currency"], "USD")
        self.assertEqual(report["opening_minor"], 2000)
        self.assertTrue(all(row["currency"] == "USD" for row in report["lines"]
                            + report["invoice_walks"]))

    def test_other_parties_and_reversed_before_window_are_excluded(self):
        self.invoice("i1", party="a")
        self.invoice("i2", party="b")
        self.invoice("old", issued="2025-12-01", due="2025-12-20")
        self.w.ledger.reverse_document("rold", "old", "2025-12-10", "void")
        report = self.w.bridges.build("br", ["a"], "CNY", "2026-02-01", "2026-03-01")
        self.assertEqual([row["invoice"] for row in report["invoice_walks"]], ["i1"])

    # ------------------------------------------------------------- freeze / versions
    def test_freeze_is_idempotent_and_revision_keeps_issued_version_intact(self):
        self.invoice("i1")
        v1 = self.w.bridges.freeze("br", ["a"], "CNY", "2026-02-01", "2026-03-01",
                                   "auditor")
        self.assertEqual(v1["version"], 1)
        again = self.w.bridges.freeze("br", ["a"], "CNY", "2026-02-01", "2026-03-01",
                                      "auditor")
        self.assertEqual(again["version"], 1)
        self.assertEqual(len(self.w.bridges.history("br")["versions"]), 1)
        # A correction after reopening produces a new version.
        self.invoice("i2", amount="50", issued="2026-02-03", due="2026-02-20")
        v2 = self.w.bridges.revise("br", "auditor", v1["digest"])
        self.assertEqual(v2["version"], 2)
        self.assertEqual(v2["supersedes"], 1)
        stored_v1 = self.w.bridges.get("br", 1)
        self.assertEqual(stored_v1["closing_minor"], v1["closing_minor"])
        self.assertNotEqual(v1["digest"], v2["digest"])
        # Re-issuing the same frozen evidence stays idempotent at the new head.
        head = self.w.bridges.revise("br", "auditor", v2["digest"])
        self.assertEqual(head["version"], 2)
        with self.assertRaises(BookError) as caught:
            self.w.bridges.revise("br", "auditor", "0" * 64)
        self.assertEqual(caught.exception.code, "conflict")

    def test_scope_cannot_change_between_versions(self):
        self.invoice("i1", party="a")
        self.invoice("i2", party="b")
        v1 = self.w.bridges.freeze("br", ["a"], "CNY", "2026-02-01", "2026-03-01",
                                   "auditor")
        with self.assertRaises(BookError) as caught:
            self.w.bridges.freeze("br", ["a", "b"], "CNY", "2026-02-01",
                                  "2026-03-01", "auditor", v1["digest"])
        self.assertEqual(caught.exception.code, "conflict")

    def test_evidence_drills_from_line_to_ledger_records(self):
        self.invoice("i1")
        self.payment("p1", "60")
        self.w.ledger.allocate("al1", "p1", "i1", "60", "2026-02-11")
        self.w.bridges.freeze("br", ["a"], "CNY", "2026-02-01", "2026-03-01", "auditor")
        line = self.w.bridges.evidence("br", "i1:cash_collection:al1")
        kinds = {record["kind"] for record in line["supporting_records"]}
        self.assertEqual(kinds, {"allocation", "source_document"})
        self.assertEqual(line["evidence"]["allocation"], "al1")
        with self.assertRaises(BookError) as caught:
            self.w.bridges.evidence("br", "missing:line")
        self.assertEqual(caught.exception.code, "missing")

    def test_export_roundtrip_digest_and_tamper_detection(self):
        self.invoice("i1")
        self.w.bridges.freeze("br", ["a"], "CNY", "2026-02-01", "2026-03-01", "auditor")
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "bridge.json"
            result = self.w.bridges.export("br", target)
            verified = self.w.bridges.verify(target)
            self.assertTrue(verified["verified"])
            self.assertEqual(result["digest"], verified["digest"])
            with self.assertRaises(BookError):
                self.w.bridges.export("br", target)
            # Flip an internal control flag while keeping the JSON valid.
            original = target.read_text()
            tampered = original.replace('"passed":true', '"passed":false', 1)
            self.assertNotEqual(tampered, original)
            target.write_text(tampered)
            with self.assertRaises(BookError):
                self.w.bridges.verify(target)

    def test_dispatch_exposes_bridge_actions(self):
        self.invoice("i1")
        result = self.w.dispatch({
            "action": "bridge.build",
            "arguments": {
                "bridge_id": "br", "parties": ["a"], "code": "CNY",
                "start": "2026-02-01", "end": "2026-03-01",
            },
        })
        self.assertTrue(result["checks"]["passed"])
        frozen = self.w.dispatch({
            "action": "bridge.freeze",
            "arguments": {
                "bridge_id": "br", "parties": ["a"], "code": "CNY",
                "start": "2026-02-01", "end": "2026-03-01", "operator": "auditor",
            },
        })
        self.assertEqual(frozen["version"], 1)
        with self.assertRaises(BookError):
            self.w.dispatch({"action": "bridge.unknown"})

    def test_require_controls_runs_ledger_integrity_checks(self):
        self.invoice("i1")
        report = self.w.bridges.build(
            "br", ["a"], "CNY", "2026-02-01", "2026-03-01", require_controls=True
        )
        self.assertTrue(report["checks"]["passed"])
        self.w.db.connection.execute(
            "UPDATE documents SET digest='bad' WHERE id='i1'"
        )
        with self.assertRaises(BookError) as caught:
            self.w.bridges.build(
                "br", ["a"], "CNY", "2026-02-01", "2026-03-01", require_controls=True
            )
        self.assertEqual(caught.exception.code, "integrity")


if __name__ == "__main__":
    unittest.main()
