from .books.database import Database
from .books.ledger import Ledger
from .books.disputes import Disputes
from .books.controls import Controls
from .books.periods import Periods
from .importers.documents import DocumentImporter
from .importers.bank import BankEvidence
from .matching.planner import Matcher
from .reporting.aging import Aging
from .reporting.statements import Statements
from .reporting.queue import CollectionQueue
from .primitives import obj, error
from .importers.inbox import Inbox
from .importers.profiles import ImportProfiles
from .matching.subsets import SubsetMatcher
from .reporting.confirmation import Confirmations
from .reporting.delivery import JournalDelivery
from .reporting.trace import Trace
from .reporting.bridge import MovementBridge


class Workbench:
    def __init__(self, path=":memory:"):
        self.db = Database(path)
        self.ledger = Ledger(self.db)
        self.documents = DocumentImporter(self.db, self.ledger)
        self.bank = BankEvidence(self.db, self.ledger)
        self.matching = Matcher(self.db, self.ledger)
        self.disputes = Disputes(self.db, self.ledger)
        self.aging = Aging(self.db, self.ledger)
        self.controls = Controls(self.db, self.ledger)
        self.periods = Periods(self.db, self.ledger, self.controls)
        self.statements = Statements(self.db, self.ledger, self.aging)
        self.queue = CollectionQueue(self.db, self.ledger, self.aging)
        self.inbox = Inbox(self.db, self.documents)
        self.profiles = ImportProfiles(self.db, self.documents, self.bank)
        self.subsets = SubsetMatcher(self.db, self.ledger)
        self.confirmations = Confirmations(self.db, self.ledger, self.aging)
        self.delivery = JournalDelivery(self.db)
        self.trace = Trace(self.db, self.ledger)
        self.bridges = MovementBridge(self.db, self.ledger, self.aging, self.controls)

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def dispatch(self, request):
        obj(request, {"action", "arguments"}, {"action"})
        arguments = request.get("arguments", {})
        obj(arguments)
        actions = {
            "party.register": self.ledger.register_party,
            "document.post": self.ledger.post,
            "document.get": self.ledger.document,
            "document.balance": self.ledger.balance,
            "document.reverse": self.ledger.reverse_document,
            "batch.import": self.documents.import_batch,
            "allocation.create": self.ledger.allocate,
            "allocation.reverse": self.ledger.reverse_allocation,
            "bank.add": self.bank.add,
            "bank.candidates": self.bank.candidates,
            "bank.link": self.bank.link,
            "bank.unlink": self.bank.unlink,
            "bank.unmatched": self.bank.unmatched,
            "bank.reconcile": self.bank.reconcile,
            "matching.candidates": self.matching.candidates,
            "matching.propose": self.matching.propose,
            "matching.confirm": self.matching.confirm,
            "matching.cancel": self.matching.cancel,
            "matching.get": self.matching.get,
            "aging.report": self.aging.report,
            "aging.unapplied": self.aging.unapplied,
            "aging.movement": self.aging.movement,
            "dispute.open": self.disputes.open,
            "dispute.close": self.disputes.close,
            "dispute.list": self.disputes.list,
            "statement.freeze": self.statements.freeze,
            "statement.get": self.statements.get,
            "statement.compare": self.statements.compare,
            "statement.export": self.statements.export,
            "statement.csv": self.statements.csv,
            "period.create": self.periods.create,
            "period.preview": self.periods.preview,
            "period.close": self.periods.close,
            "period.verify": self.periods.verify,
            "control.inspect": self.controls.inspect,
            "control.trial_balance": self.controls.trial_balance,
            "control.subledger": self.controls.subledger,
            "control.journal": self.controls.journal,
            "queue.build": self.queue.build,
            "queue.exposure": self.queue.exposure,
            "queue.forecast": self.queue.forecast,
            "queue.exceptions": self.queue.exceptions,
        }
        for prefix, component, methods in [
            ("trace", self.trace, ["document", "provenance", "allocation_graph", "audit_page"]),
            ("inbox", self.inbox, ["stage", "get", "inspect", "approve", "commit", "reject", "pending"]),
            ("profile", self.profiles, ["save", "get", "preview", "apply"]),
            ("subset", self.subsets, ["search", "propose"]),
            ("confirmation", self.confirmations, ["submit", "get", "reconcile", "decide", "close"]),
            ("delivery", self.delivery, ["register", "status", "lease", "release", "prepare", "get", "resume", "acknowledge", "verify_checkpoint", "envelope", "verify_envelope"]),
            ("bridge", self.bridges, ["build", "freeze", "revise", "get", "history", "evidence", "export", "verify"]),
        ]:
            for method in methods:
                actions[prefix + "." + method] = getattr(component, method)
        action = request["action"]
        if not isinstance(action, str) or action not in actions:
            error("action", "unknown workbench action")
        import inspect

        try:
            inspect.signature(actions[action]).bind(**arguments)
        except TypeError as exc:
            error("input", "invalid action arguments", reason=str(exc))
        return actions[action](**arguments)
