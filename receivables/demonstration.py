from tempfile import TemporaryDirectory
from pathlib import Path
from .service import Workbench
from .primitives import BookError


def run():
    with TemporaryDirectory() as folder:
        with Workbench(Path(folder) / "book.sqlite3") as work:
            work.ledger.register_party("factory", "合成仪器厂", ["Instrument Factory"])
            for ident, kind, amount, due in [
                ("inv1", "invoice", "120.50", "2026-02-01"),
                ("inv2", "invoice", "80.00", "2026-02-10"),
                ("pay", "payment", "150.00", None),
            ]:
                row = {
                    "id": ident,
                    "kind": kind,
                    "party": "factory",
                    "currency": "CNY",
                    "amount": amount,
                    "issued": "2026-01-10",
                    "reference": "LOCAL-001",
                }
                if due:
                    row["due"] = due
                work.ledger.post(row)
            plan = work.matching.propose("plan", "pay", "2026-02-11", "oldest_due")
            assert len(plan["allocations"]) == 2
            work.matching.confirm("plan", "reviewer")
            assert work.ledger.balance("inv2") == 5050
            work.disputes.open("dispute", "inv2", "10.00", "2026-02-12", "合成质量争议")
            statement = work.statements.freeze("statement", "factory", "2026-02-28")
            assert statement["aging"]["groups"][0]["collectible_minor"] == 4050
            work.bank.add(
                {
                    "id": "bank1",
                    "account": "cash",
                    "currency": "CNY",
                    "amount": "150.00",
                    "booked": "2026-01-11",
                    "reference": "LOCAL-001",
                    "payer": "Instrument Factory",
                }
            )
            work.bank.link("bank1", "pay", "reviewer")
            work.periods.create("feb", "2026-02-01", "2026-03-01")
            preview = work.periods.preview("feb")
            work.periods.close("feb", preview["digest"], "controller")
            try:
                work.ledger.reverse_allocation(
                    "invalid", "plan:0", "2026-02-20", "late correction"
                )
            except BookError as exc:
                assert exc.code == "period"
            else:
                raise AssertionError("closed-period write accepted")
            work.ledger.reverse_allocation(
                "reverse", "plan:0", "2026-03-01", "valid subsequent correction"
            )
            assert work.periods.verify("feb")["verified"]

            # Movement bridge between the two month-end cutoffs: every difference
            # between the two balance sheets is attributed once to a ledger event.
            bridge = work.bridges.build(
                "monthly-bridge", ["factory"], "CNY", "2026-02-28", "2026-03-31"
            )
            assert bridge["checks"]["passed"]
            assert (
                bridge["opening_minor"]
                + sum(bridge["principal_bridge"]["categories_minor"].values())
                == bridge["closing_minor"]
            )
            # The allocation reversal restores the receivable; no cash moves.
            assert bridge["principal_bridge"]["categories_minor"]["write_off_reversal"] == 12050
            assert bridge["dispute_bridge"]["cash_included_minor"] == 0
            frozen = work.bridges.freeze(
                "monthly-bridge", ["factory"], "CNY",
                "2026-02-28", "2026-03-31", "controller", require_controls=True,
            )
            bridge_path = Path(folder) / "bridge.json"
            work.bridges.export("monthly-bridge", bridge_path)
            assert work.bridges.verify(bridge_path)["verified"]
            # A reopened-period correction becomes a new version; the issued one
            # stays byte-for-byte identical and remains verifiable.
            work.ledger.reverse_allocation(
                "reverse-2", "plan:1", "2026-03-02", "second reopened correction"
            )
            revision = work.bridges.revise(
                "monthly-bridge", "controller", frozen["digest"], require_controls=True
            )
            assert revision["version"] == 2 and revision["supersedes"] == 1
            assert work.bridges.get("monthly-bridge", 1)["digest"] == frozen["digest"]
            assert revision["closing_minor"] != frozen["closing_minor"]
            # A reviewer can drill from any summary line to the ledger evidence.
            evidence = work.bridges.evidence(
                "monthly-bridge", "inv1:write_off_reversal:reverse"
            )
            assert {record["kind"] for record in evidence["supporting_records"]} >= {
                "allocation",
                "reversal",
            }

            work.statements.export("statement", Path(folder) / "statement.json")
            assert work.statements.verify(Path(folder) / "statement.json")["verified"]
            assert work.controls.require_valid()["ok"]
            return {
                "status": "ok",
                "historical_open_minor": 5050,
                "current_open_minor": work.ledger.balance("inv1")
                + work.ledger.balance("inv2"),
                "closed_period_verified": True,
                "bridge_opening_minor": bridge["opening_minor"],
                "bridge_closing_minor": bridge["closing_minor"],
                "bridge_versions": work.bridges.history("monthly-bridge")["versions"],
                "offline": True,
            }
