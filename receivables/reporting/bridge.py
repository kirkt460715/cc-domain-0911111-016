import os
from datetime import date
from pathlib import Path
from ..primitives import (
    identifier,
    day,
    currency,
    display,
    integer,
    sequence,
    canonical,
    digest,
    decode,
    error,
)

DDL = """
CREATE TABLE IF NOT EXISTS bridges(id TEXT NOT NULL,version INTEGER NOT NULL,
 scope_digest TEXT NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL,
 created TEXT NOT NULL,operator TEXT NOT NULL,supersedes INTEGER,
 PRIMARY KEY(id,version));
"""

BUCKETS = ["not_due", "1_30", "31_60", "61_90", "over_90"]
PRINCIPAL_CATEGORIES = (
    "new_confirmation",
    "cash_collection",
    "credit_memo",
    "write_off",
    "write_off_reversal",
    "aging_migration",
)
DISPUTE_CATEGORY = "dispute_change"
CASH_CATEGORIES = {"cash_collection"}
# Ordering is part of the frozen contract: lines keep stable identifiers,
# so reviewers can quote "line 3" across revisions and exported files.
KIND_RANK = {
    "new_confirmation": 0,
    "cash_collection": 1,
    "credit_memo": 2,
    "write_off": 3,
    "write_off_reversal": 4,
    "aging_migration": 5,
    "dispute_open": 6,
    "dispute_close": 7,
    "dispute_reassessment": 8,
}


class MovementBridge:
    """Explains receivable movement between two cutoffs from ledger events.

    The principal bridge reconciles gross receivable: opening plus the five
    exposure-changing categories plus zero-net aging migration equals closing.
    A second, explicitly non-cash layer reconciles the disputed slice of that
    same gross exposure. Every amount is attributed once, to one invoice and
    (for principal lines) one aging bucket, so every slice stays conserved.
    """

    def __init__(self, database, ledger, aging, controls=None):
        self.db = database
        self.ledger = ledger
        self.aging = aging
        self.controls = controls
        self.db.connection.executescript(DDL)

    # ------------------------------------------------------------------ scope
    def _scope(self, parties, code, start, end):
        identifier_list = sorted({identifier(value) for value in sequence(parties, 1, 1000)})
        for party in identifier_list:
            self.ledger.party(party)
        code = currency(code)
        start, end = day(start), day(end)
        if end <= start:
            error("input", "bridge interval must increase")
        return {"parties": identifier_list, "currency": code, "start": start, "end": end}

    def _invoices(self, scope):
        placeholders = ",".join("?" for _ in scope["parties"])
        return self.db.rows(
            "SELECT * FROM documents WHERE kind='invoice' AND currency=? AND party IN ("
            + placeholders
            + ") ORDER BY id",
            (scope["currency"], *scope["parties"]),
        )

    @staticmethod
    def _bucket(due, as_of):
        late = (date.fromisoformat(as_of) - date.fromisoformat(due)).days
        if late <= 0:
            return "not_due"
        if late <= 30:
            return "1_30"
        if late <= 60:
            return "31_60"
        if late <= 90:
            return "61_90"
        return "over_90"

    # --------------------------------------------------------------- building
    def build(self, bridge_id, parties, code, start, end, require_controls=False):
        identifier(bridge_id)
        scope = self._scope(parties, code, start, end)
        if require_controls:
            if self.controls is None:
                error("state", "control checks are not wired")
            self.controls.require_valid()
        code = scope["currency"]
        lines = []
        dispute_lines = []
        walks = []
        opening_total = closing_total = 0
        opening_disputed_total = closing_disputed_total = 0

        for invoice in self._invoices(scope):
            opening = self.ledger.balance(invoice["id"], start)
            closing = self.ledger.balance(invoice["id"], end)
            walk = self._invoice_walk(invoice, scope, opening, lines)
            if walk is None:
                # Reversed before the window with no outstanding exposure:
                # it cannot explain any movement between the two cutoffs.
                continue
            open_dispute = min(opening, self.aging.disputed(invoice["id"], start))
            close_dispute = min(closing, self.aging.disputed(invoice["id"], end))
            self._dispute_walk(invoice, scope, opening, closing, open_dispute,
                               close_dispute, dispute_lines, walk)
            if walk["balance_minor"] != closing:
                error("integrity", "bridge invoice walk does not reach closing",
                      invoice=invoice["id"])
            if walk["opening_minor"] != opening:
                error("integrity", "bridge invoice walk opening differs",
                      invoice=invoice["id"])
            walks.append(walk)
            opening_total += opening
            closing_total += closing
            opening_disputed_total += open_dispute
            closing_disputed_total += close_dispute

        lines.sort(key=lambda row: (row["party"], row["invoice"], row["seq"], row["line_id"]))
        dispute_lines.sort(
            key=lambda row: (row["party"], row["invoice"], row["seq"], row["line_id"])
        )
        for index, row in enumerate(lines, 1):
            row["line_no"] = index
        for index, row in enumerate(dispute_lines, 1):
            row["line_no"] = index

        unapplied = [
            row
            for row in self.aging.unapplied(end)
            if row["currency"] == code and row["party"] in set(scope["parties"])
        ]
        payload = {
            "format_version": 1,
            "id": bridge_id,
            "scope": scope,
            "scope_digest": digest(scope),
            "opening_minor": opening_total,
            "closing_minor": closing_total,
            "opening_disputed_minor": opening_disputed_total,
            "closing_disputed_minor": closing_disputed_total,
            "unapplied_sources": unapplied,
            "principal_bridge": self._summarize(lines, code),
            "dispute_bridge": self._dispute_summary(dispute_lines, code),
            "lines": lines,
            "dispute_lines": dispute_lines,
            "invoice_walks": walks,
        }
        payload["checks"] = self._verify_payload(payload)
        return payload

    def _invoice_walk(self, invoice, scope, opening, lines):
        start, end = scope["start"], scope["end"]
        ident = invoice["id"]
        # Invoices reversed inside the window still carry the write-off event
        # and the dispute reclassification, so they must not be skipped merely
        # because they are inactive at the closing cutoff.
        current = opening
        current_bucket = self._bucket(invoice["due"], start) if current else None
        events = []

        # Invoice issued inside the window creates the whole exposure.
        if start < invoice["issued"] <= end:
            events.append(
                {
                    "date": invoice["issued"],
                    "kind": "new_confirmation",
                    "amount_minor": invoice["amount"],
                    "event_id": ident,
                    "evidence": {"document": ident},
                }
            )

        for allocation in self.db.rows(
            "SELECT * FROM allocations WHERE invoice=? ORDER BY effective,id", (ident,)
        ):
            created_in_window = start < allocation["effective"] <= end
            open_at_start = (
                allocation["effective"] <= start
                and (allocation["reversed"] is None or allocation["reversed"] > start)
            )
            reversal = allocation["reversed"]
            reversed_in_window = bool(reversal) and start < reversal <= end
            if created_in_window:
                source = self.ledger.document(allocation["source"])
                events.append(
                    {
                        "date": allocation["effective"],
                        "kind": "cash_collection"
                        if source["kind"] == "payment"
                        else "credit_memo",
                        "amount_minor": -allocation["amount"],
                        "event_id": allocation["id"],
                        "evidence": {
                            "allocation": allocation["id"],
                            "source_document": allocation["source"],
                            "invoice": ident,
                        },
                    }
                )
            if reversed_in_window:
                reversal_row = self.db.one(
                    "SELECT id FROM reversals WHERE target=? AND kind='allocation'",
                    (allocation["id"],),
                )
                # Every allocation reversal restores receivable that had been
                # written off through matching, whether the source was cash or
                # a credit note. The distinction stays visible in the evidence.
                events.append(
                    {
                        "date": reversal,
                        "kind": "write_off_reversal",
                        "amount_minor": allocation["amount"],
                        "event_id": (reversal_row or {}).get("id", allocation["id"]),
                        "evidence": {
                            "reversal": (reversal_row or {}).get("id"),
                            "allocation": allocation["id"],
                            "source_document": allocation["source"],
                            "invoice": ident,
                        },
                    }
                )

        reversal = self.db.one(
            "SELECT * FROM document_reversals WHERE document=?", (ident,)
        )
        if reversal and start < reversal["effective"] <= end:
            events.append(
                {
                    "date": reversal["effective"],
                    "kind": "write_off",
                    "amount_minor": -invoice["amount"],
                    "event_id": reversal["reversal"],
                    "evidence": {
                        "reversal": reversal["reversal"],
                        "document": ident,
                    },
                }
            )

        events.sort(key=lambda row: (row["date"], KIND_RANK[row["kind"]], row["event_id"]))
        emitted = []
        step = 0
        for event in events:
            # Time passage moves the standing balance between buckets first;
            # migration never changes the exposure itself.
            if current:
                target_bucket = self._bucket(invoice["due"], event["date"])
                step = self._migrate(
                    invoice, event["date"], current_bucket, target_bucket,
                    current, step, lines, emitted,
                )
                current_bucket = target_bucket
            amount = event["amount_minor"]
            bucket = self._bucket(invoice["due"], event["date"])
            line = {
                "line_id": ident + ":" + event["kind"] + ":" + event["event_id"],
                "party": invoice["party"],
                "invoice": ident,
                "currency": invoice["currency"],
                "date": event["date"],
                "category": event["kind"],
                "bucket": bucket,
                "amount_minor": amount,
                "amount": display(amount, invoice["currency"]),
                "cash": event["kind"] in CASH_CATEGORIES,
                "evidence": event["evidence"],
                "seq": step,
            }
            lines.append(line)
            emitted.append(line)
            current += amount
            if current < 0:
                error("integrity", "bridge walk drove invoice balance negative",
                      invoice=ident)
            current_bucket = bucket if current else None

        if current:
            target_bucket = self._bucket(invoice["due"], end)
            step = self._migrate(
                invoice, end, current_bucket, target_bucket,
                current, step, lines, emitted,
            )
            current_bucket = target_bucket

        if not opening and not current and not emitted:
            # No exposure at either cutoff and no event between them.
            return None

        return {
            "invoice": ident,
            "party": invoice["party"],
            "currency": invoice["currency"],
            "issued": invoice["issued"],
            "due": invoice["due"],
            "opening_minor": opening,
            "closing_minor": self.ledger.balance(ident, end),
            "opening_bucket": self._bucket(invoice["due"], start) if opening else None,
            "closing_bucket": current_bucket,
            "balance_minor": current,
            "line_ids": [row["line_id"] for row in emitted],
        }

    def _migrate(self, invoice, as_of, from_bucket, to_bucket, standing,
                 step, lines, emitted):
        if not standing or from_bucket == to_bucket:
            return step
        if BUCKETS.index(to_bucket) < BUCKETS.index(from_bucket):
            error("integrity", "aging can only move to older buckets",
                  invoice=invoice["id"])
        cursor = BUCKETS.index(from_bucket)
        target = BUCKETS.index(to_bucket)
        while cursor < target:
            step += 1
            line = {
                "line_id": invoice["id"] + ":aging_migration:" + str(step),
                "party": invoice["party"],
                "invoice": invoice["id"],
                "currency": invoice["currency"],
                "date": as_of,
                "category": "aging_migration",
                "from_bucket": BUCKETS[cursor],
                "bucket": BUCKETS[cursor + 1],
                "to_bucket": BUCKETS[cursor + 1],
                "amount_minor": 0,
                "moved_minor": standing,
                "moved": display(standing, invoice["currency"]),
                "amount": display(0, invoice["currency"]),
                "cash": False,
                "evidence": {"invoice": invoice["id"], "basis": "due_date_aging"},
                "seq": step,
            }
            lines.append(line)
            emitted.append(line)
            cursor += 1
        return step

    def _dispute_walk(self, invoice, scope, opening_balance, closing_balance,
                      opening_disputed, closing_disputed, dispute_lines, walk):
        ident = invoice["id"]
        start, end = scope["start"], scope["end"]
        step = 0
        for dispute in self.db.rows(
            "SELECT * FROM disputes WHERE invoice=? ORDER BY opened,id", (ident,)
        ):
            if start < dispute["opened"] <= end:
                step += 1
                dispute_lines.append(
                    self._dispute_line(
                        invoice, dispute["opened"], "dispute_open",
                        dispute["amount"],
                        "dispute:" + dispute["id"] + ":open",
                        dispute, step,
                    )
                )
            if dispute["closed"] and start < dispute["closed"] <= end:
                step += 1
                dispute_lines.append(
                    self._dispute_line(
                        invoice, dispute["closed"], "dispute_close",
                        -dispute["amount"],
                        "dispute:" + dispute["id"] + ":close",
                        dispute, step,
                    )
                )
        raw_change = sum(row["amount_minor"] for row in dispute_lines
                         if row["invoice"] == ident)
        residual = (closing_disputed - opening_disputed) - raw_change
        if residual:
            # Disputes are capped at the outstanding principal: a collection or
            # write-off reclassifies the capped slice without any cash movement.
            step += 1
            dispute_lines.append(
                self._dispute_line(
                    invoice, end, "dispute_reassessment", residual,
                    ident + ":dispute_reassessment", None, step,
                )
            )
        total = sum(row["amount_minor"] for row in dispute_lines
                    if row["invoice"] == ident)
        if opening_disputed + total != closing_disputed:
            error("integrity", "dispute slice does not reconcile", invoice=ident)
        walk["disputed_opening_minor"] = opening_disputed
        walk["disputed_closing_minor"] = closing_disputed
        walk["dispute_line_ids"] = [
            row["line_id"] for row in dispute_lines if row["invoice"] == ident
        ]

    def _dispute_line(self, invoice, when, kind, amount, line_id, dispute, seq):
        return {
            "line_id": line_id,
            "party": invoice["party"],
            "invoice": invoice["id"],
            "currency": invoice["currency"],
            "date": when,
            "category": DISPUTE_CATEGORY,
            "event_kind": kind,
            "bucket": self._bucket(invoice["due"], when),
            "amount_minor": amount,
            "amount": display(amount, invoice["currency"]),
            "cash": False,
            "evidence": {"dispute": dispute["id"]} if dispute else
                        {"invoice": invoice["id"], "basis": "principal_cap"},
            "seq": seq,
        }

    # ------------------------------------------------------------- summaries
    def _summarize(self, lines, code):
        categories = {key: 0 for key in PRINCIPAL_CATEGORIES}
        parties = {}
        buckets = {key: {"in_minor": 0, "out_minor": 0} for key in BUCKETS}
        for row in lines:
            if row["category"] == "aging_migration":
                buckets[row["from_bucket"]]["out_minor"] += row["moved_minor"]
                buckets[row["to_bucket"]]["in_minor"] += row["moved_minor"]
                continue
            categories[row["category"]] += row["amount_minor"]
            party = parties.setdefault(
                row["party"], {key: 0 for key in PRINCIPAL_CATEGORIES}
            )
            party[row["category"]] += row["amount_minor"]
        result = {
            "categories_minor": categories,
            "categories": {key: display(value, code) for key, value in categories.items()},
            "cash_collection_minor": categories["cash_collection"],
            "non_cash_change_minor": sum(
                value
                for key, value in categories.items()
                if key not in CASH_CATEGORIES
            ),
            "aging_flows_minor": buckets,
            "by_party_minor": parties,
        }
        return result

    def _dispute_summary(self, lines, code):
        total = sum(row["amount_minor"] for row in lines)
        by_party = {}
        by_kind = {"dispute_open": 0, "dispute_close": 0, "dispute_reassessment": 0}
        for row in lines:
            by_party[row["party"]] = by_party.get(row["party"], 0) + row["amount_minor"]
            by_kind[row["event_kind"]] += row["amount_minor"]
        return {
            "change_minor": total,
            "change": display(total, code),
            "cash_included_minor": 0,
            "by_event_kind_minor": by_kind,
            "by_party_minor": by_party,
        }

    def _verify_payload(self, payload):
        start = payload["opening_minor"]
        categories = payload["principal_bridge"]["categories_minor"]
        subtotal = sum(categories.values())
        checks = []

        def record(name, ok, detail=None):
            checks.append({"name": name, "passed": bool(ok), "detail": detail})
            if not ok:
                error("integrity", "bridge conservation check failed", check=name,
                      detail=detail)

        record(
            "principal_total",
            start + subtotal == payload["closing_minor"],
            {"opening_minor": start, "movement_minor": subtotal,
             "closing_minor": payload["closing_minor"]},
        )
        migration = payload["principal_bridge"]["aging_flows_minor"]
        record(
            "aging_zero_net",
            sum(b["in_minor"] for b in migration.values())
            == sum(b["out_minor"] for b in migration.values()),
        )
        record(
            "dispute_is_not_cash",
            payload["dispute_bridge"]["cash_included_minor"] == 0
            and all(not row["cash"] for row in payload["dispute_lines"]),
        )
        record(
            "dispute_total",
            payload["opening_disputed_minor"]
            + payload["dispute_bridge"]["change_minor"]
            == payload["closing_disputed_minor"],
        )
        record(
            "dispute_within_principal",
            payload["closing_disputed_minor"] <= payload["closing_minor"]
            and payload["opening_disputed_minor"] <= payload["opening_minor"],
        )
        # Every event is counted once: unique line identifiers.
        ids = [row["line_id"] for row in payload["lines"]]
        record("lines_counted_once", len(ids) == len(set(ids)))
        ids = [row["line_id"] for row in payload["dispute_lines"]]
        record("dispute_lines_counted_once", len(ids) == len(set(ids)))

        # Slice conservation: per party, per aging bucket and per dispute flag.
        party_slice = {}
        bucket_slice = {key: {"opening": 0, "movement": 0, "closing": 0} for key in BUCKETS}
        for walk in payload["invoice_walks"]:
            slot = party_slice.setdefault(
                walk["party"], {"opening": 0, "closing": 0, "movement": 0}
            )
            slot["opening"] += walk["opening_minor"]
            slot["closing"] += walk["closing_minor"]
            if walk["opening_bucket"]:
                bucket_slice[walk["opening_bucket"]]["opening"] += walk["opening_minor"]
            if walk["closing_bucket"]:
                bucket_slice[walk["closing_bucket"]]["closing"] += walk["closing_minor"]
        for row in payload["lines"]:
            party_slice[row["party"]]["movement"] += row["amount_minor"]
            if row["category"] != "aging_migration":
                bucket_slice[row["bucket"]]["movement"] += row["amount_minor"]
            else:
                bucket_slice[row["from_bucket"]]["movement"] -= row["moved_minor"]
                bucket_slice[row["to_bucket"]]["movement"] += row["moved_minor"]
        record(
            "party_slice_conservation",
            all(slot["opening"] + slot["movement"] == slot["closing"]
                for slot in party_slice.values())
            and sum(s["opening"] for s in party_slice.values()) == payload["opening_minor"]
            and sum(s["closing"] for s in party_slice.values()) == payload["closing_minor"],
        )
        record(
            "aging_bucket_conservation",
            all(slot["opening"] + slot["movement"] == slot["closing"]
                for slot in bucket_slice.values()),
            bucket_slice,
        )
        dispute_party = {}
        for walk in payload["invoice_walks"]:
            slot = dispute_party.setdefault(
                walk["party"], {"opening": 0, "closing": 0, "change": 0}
            )
            slot["opening"] += walk["disputed_opening_minor"]
            slot["closing"] += walk["disputed_closing_minor"]
        for row in payload["dispute_lines"]:
            dispute_party.setdefault(
                row["party"], {"opening": 0, "closing": 0, "change": 0}
            )["change"] += row["amount_minor"]
        record(
            "dispute_party_conservation",
            all(
                slot["opening"] + slot["change"] == slot["closing"]
                for slot in dispute_party.values()
            ),
            dispute_party,
        )
        return {"passed": all(row["passed"] for row in checks), "checks": checks}

    # ------------------------------------------------------------- freeze / versions
    def freeze(self, bridge_id, parties, code, start, end, operator,
               expected_digest=None, require_controls=False):
        identifier(operator)
        payload = self.build(bridge_id, parties, code, start, end, require_controls)
        with self.db.transaction() as con:
            previous = self.db.one(
                "SELECT version,digest,scope_digest FROM bridges WHERE id=? "
                "ORDER BY version DESC LIMIT 1",
                (bridge_id,),
            )
            fingerprint = digest(payload)
            if previous is None:
                if expected_digest is not None:
                    error("conflict", "bridge does not exist yet")
                version, supersedes = 1, None
            elif previous["digest"] == fingerprint:
                # Idempotent replay of the same evidence returns the frozen
                # version instead of minting a duplicate. An explicit stale
                # digest is still rejected below.
                if expected_digest is not None and expected_digest != previous["digest"]:
                    error("conflict", "bridge revision digest is stale")
                return self.get(bridge_id, previous["version"])
            else:
                if expected_digest != previous["digest"]:
                    # Optimistic concurrency: corrections must quote the digest
                    # of the latest issued version.
                    error(
                        "conflict",
                        "bridge evidence changed; revise against the latest digest",
                    )
                if previous["scope_digest"] != payload["scope_digest"]:
                    error("conflict", "bridge scope is immutable across versions")
                version = previous["version"] + 1
                supersedes = previous["version"]
            con.execute(
                "INSERT INTO bridges VALUES(?,?,?,?,?,?,?,?)",
                (
                    bridge_id,
                    version,
                    payload["scope_digest"],
                    canonical(payload),
                    fingerprint,
                    self._today(),
                    operator,
                    supersedes,
                ),
            )
            self.db.audit(
                "bridge_frozen",
                bridge_id,
                {
                    "version": version,
                    "scope_digest": payload["scope_digest"],
                    "digest": fingerprint,
                    "operator": operator,
                    "supersedes": supersedes,
                },
            )
        return self.get(bridge_id, version)

    def revise(self, bridge_id, operator, expected_digest, require_controls=False):
        # Reopen-and-correct entry point: rebuild from current ledger evidence
        # and store a new version; every previously issued version stays intact.
        row = self.db.one(
            "SELECT payload FROM bridges WHERE id=? ORDER BY version DESC LIMIT 1",
            (bridge_id,),
        )
        if row is None:
            error("missing", "bridge not found")
        scope = decode(row["payload"])["scope"]
        return self.freeze(
            bridge_id,
            scope["parties"],
            scope["currency"],
            scope["start"],
            scope["end"],
            operator,
            expected_digest,
            require_controls,
        )

    def get(self, bridge_id, version=None):
        if version is not None:
            integer(version, 1)
        row = self.db.one(
            "SELECT * FROM bridges WHERE id=? ORDER BY version DESC LIMIT 1"
            if version is None
            else "SELECT * FROM bridges WHERE id=? AND version=?",
            (bridge_id,) if version is None else (bridge_id, version),
        )
        if row is None:
            error("missing", "bridge version not found")
        payload = decode(row["payload"])
        if digest(payload) != row["digest"]:
            error("integrity", "bridge fingerprint differs")
        return {
            **payload,
            "version": row["version"],
            "supersedes": row["supersedes"],
            "created": row["created"],
            "operator": row["operator"],
            "digest": row["digest"],
        }

    def history(self, bridge_id):
        rows = self.db.rows(
            "SELECT id,version,scope_digest,digest,created,operator,supersedes "
            "FROM bridges WHERE id=? ORDER BY version",
            (bridge_id,),
        )
        if not rows:
            error("missing", "bridge not found")
        return {"id": bridge_id, "versions": rows}

    def evidence(self, bridge_id, line_id, version=None):
        """Drill from a bridge summary line to the underlying ledger evidence."""
        report = self.get(bridge_id, version)
        for group in ("lines", "dispute_lines"):
            for row in report[group]:
                if row["line_id"] == line_id:
                    found = dict(row)
                    found["supporting_records"] = self._supporting(row["evidence"])
                    return found
        error("missing", "bridge line not found", line=line_id)

    def _supporting(self, evidence):
        records = []
        if evidence.get("document"):
            records.append({"kind": "document", "record": self.ledger.document(evidence["document"])})
        if evidence.get("allocation"):
            records.append({
                "kind": "allocation",
                "record": self.db.one(
                    "SELECT * FROM allocations WHERE id=?", (evidence["allocation"],)
                ),
            })
        if evidence.get("reversal"):
            records.append({
                "kind": "reversal",
                "record": self.db.one(
                    "SELECT * FROM reversals WHERE id=?", (evidence["reversal"],)
                ),
            })
        if evidence.get("source_document"):
            records.append({
                "kind": "source_document",
                "record": self.ledger.document(evidence["source_document"]),
            })
        if evidence.get("dispute"):
            records.append({
                "kind": "dispute",
                "record": self.db.one(
                    "SELECT * FROM disputes WHERE id=?", (evidence["dispute"],)
                ),
            })
        if evidence.get("invoice") and evidence.get("basis") == "due_date_aging":
            document = self.ledger.document(evidence["invoice"])
            records.append({
                "kind": "aging_basis",
                "record": {"invoice": evidence["invoice"], "due": document["due"]},
            })
        if evidence.get("invoice") and evidence.get("basis") == "principal_cap":
            records.append({
                "kind": "principal_cap",
                "record": {
                    "invoice": evidence["invoice"],
                    "outstanding_minor": self.ledger.balance(evidence["invoice"]),
                },
            })
        return records

    # --------------------------------------------------------------- export
    def export(self, bridge_id, path, version=None):
        payload = self.get(bridge_id, version)
        target = Path(path)
        if target.exists() or target.is_symlink():
            error("conflict", "export destination exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".pending")
        # The digest sits beside the payload it describes, not inside it.
        body = {key: value for key, value in payload.items() if key != "digest"}
        envelope = {
            "id": bridge_id,
            "version": payload["version"],
            "payload": body,
            "digest": digest(body),
            "format_version": 1,
        }
        content = canonical(envelope).encode("utf-8")
        try:
            with temporary.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temporary, target)
        except FileExistsError:
            error("conflict", "export or staging destination exists")
        finally:
            if temporary.exists() and temporary.read_bytes() == content:
                temporary.unlink()
        return {"path": str(target), "digest": envelope["digest"], "bytes": len(content)}

    @staticmethod
    def verify(path):
        path = Path(path)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 50_000_000:
            error("path", "invalid bridge file")
        envelope = decode(path.read_text("utf-8"))
        if not isinstance(envelope, dict) or {"id", "version", "digest", "payload"} - envelope.keys():
            error("integrity", "bridge envelope missing fields")
        identifier(envelope["id"])
        integer(envelope["version"], 1)
        if envelope.get("format_version") != 1:
            error("integrity", "unsupported bridge version")
        if digest(envelope["payload"]) != envelope["digest"]:
            error("integrity", "exported bridge differs")
        return {
            "id": envelope["id"],
            "version": envelope["version"],
            "digest": envelope["digest"],
            "verified": True,
        }
    @staticmethod
    def _today():
        return date.today().isoformat()
