"""Shared service infrastructure: transactions, ids, clocks, state moves,
documents, notifications and checkout expiry."""
import json
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from ..domain import states as S
from ..store.db import Database, now_iso


class ServiceError(Exception):
    def __init__(self, http_status: int, code: str, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.http_status, self.code, self.message, self.details = http_status, code, message, details or {}


PAY_LATER, ACCOUNT = "PAY_LATER", "ACCOUNT"
APPLICANT_FIELDS = ["customer_profile", "has_formal_cic_history", "has_observable_tx", "monthly_inflow_vnd", "inflow_cv",
                    "unified_credit_score", "casa_balance_vnd", "dti_ratio", "on_time_bill_ratio", "e_wallet_topups_pm",
                    "regular_weekly_pattern", "rapid_drain_flag"]


def _j(value: Optional[str], default: Any = None) -> Any:
    return json.loads(value) if value else default


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BaseService:
    def __init__(self, db: Database, policy: Dict[str, Any], fixtures: Dict[str, Any],
                 payment_delay_ms: Optional[int] = None, clock: Callable[[], datetime] = _utcnow):
        self.db = db
        self.policy = policy
        self.fixtures = fixtures
        self.demo_date = datetime.strptime(fixtures["demo_date"], "%Y-%m-%d").date()
        self.delay_ms = policy["payment_simulation"]["processing_delay_ms"] if payment_delay_ms is None else payment_delay_ms
        self.clock = clock  # wall clock (checkout timer); tests may replace it

    # ------------------------------------------------------------ clocks
    @property
    def as_of(self) -> date:
        """Simulated business date (loan due dates, freshness). Advanced only by the demo control."""
        v = self.db.control("business_date")
        return datetime.strptime(v, "%Y-%m-%d").date() if v else self.demo_date

    def _deadline(self) -> str:
        return (self.clock() + timedelta(seconds=self.policy["checkout"]["timeout_seconds"])).isoformat(timespec="seconds")

    # ------------------------------------------------------------ infra
    @contextmanager
    def _unit(self):
        with self.db.lock:
            self.db.begin()
            try:
                yield
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise

    def _new_id(self, prefix: str, counter: str) -> str:
        return "%s-%s-%04d" % (prefix, self.demo_date.strftime("%y%m%d"), self.db.next_id(counter))

    def _tx(self, tx_id: str, customer_id: Optional[str] = None) -> Dict[str, Any]:
        tx = self.db.one("SELECT * FROM transactions WHERE transaction_id = ?", (tx_id,))
        if not tx:
            raise ServiceError(404, "TRANSACTION_NOT_FOUND", "Transaction %s not found." % tx_id)
        if customer_id is not None and tx["customer_id"] != customer_id:
            raise ServiceError(403, "FORBIDDEN", "This transaction belongs to another demo customer.")
        return tx

    def _app(self, app_id: str, customer_id: Optional[str] = None) -> Dict[str, Any]:
        app = self.db.one("SELECT * FROM applications WHERE application_id = ?", (app_id,))
        if not app:
            raise ServiceError(404, "APPLICATION_NOT_FOUND", "Application %s not found." % app_id)
        if customer_id is not None and app["customer_id"] != customer_id:
            raise ServiceError(403, "FORBIDDEN", "This application belongs to another demo customer.")
        return app

    def _app_for_tx(self, tx_id: str) -> Optional[Dict[str, Any]]:
        return self.db.one("SELECT * FROM applications WHERE transaction_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                           (tx_id,))

    def _customer(self, customer_id: str) -> Dict[str, Any]:
        c = self.db.one("SELECT * FROM customers WHERE customer_id = ?", (customer_id,))
        if not c:
            raise ServiceError(404, "CUSTOMER_NOT_FOUND", "Demo customer %s not found." % customer_id)
        return c

    def _move_tx(self, tx: Dict[str, Any], target: str, actor: str, actor_id: Optional[str],
                 extra: Optional[Dict[str, Any]] = None, details: Optional[Dict[str, Any]] = None,
                 event: str = "TRANSACTION_STATE_CHANGED") -> Dict[str, Any]:
        try:
            S.check_tx(tx["state"], target)
        except S.InvalidTransition as e:
            raise ServiceError(409, "INVALID_TRANSACTION_STATE", str(e), {"current_state": tx["state"]})
        fields = {"state": target}
        # Timer: customer-side steps get a 5-minute window; review / document
        # gathering pauses it; terminal states clear it.
        if target in ("TERMS_PENDING_CONFIRMATION",):
            fields["expires_at"] = self._deadline()
        elif target not in S.TX_EXPIRABLE:
            fields["expires_at"] = None
        fields.update(extra or {})
        if self.db.update("transactions", "transaction_id", tx["transaction_id"], fields, tx["version"]) != 1:
            raise ServiceError(409, "CONCURRENT_UPDATE", "The transaction changed in the meantime. Please refresh.")
        self.db.audit(actor, actor_id, event, tx["transaction_id"], None, tx["state"], target, details=details)
        return self._tx(tx["transaction_id"])

    def _move_credit(self, app: Dict[str, Any], target: str, actor: str, actor_id: Optional[str], action: str,
                     rule_ids: Optional[List[str]] = None, note: Optional[str] = None,
                     extra: Optional[Dict[str, Any]] = None, expected_version: Optional[int] = None) -> Dict[str, Any]:
        try:
            S.check_credit(app["credit_state"], target)
        except S.InvalidTransition as e:
            raise ServiceError(409, "INVALID_CREDIT_STATE", str(e), {"current_state": app["credit_state"]})
        fields = {"credit_state": target}
        fields.update(extra or {})
        version = app["version"] if expected_version is None else expected_version
        if self.db.update("applications", "application_id", app["application_id"], fields, version) != 1:
            raise ServiceError(409, "STALE_DECISION", "The case was updated by someone else. Reload before acting.")
        self.db.run("INSERT INTO decisions(application_id, ts, actor_type, actor_id, action, from_state, to_state, "
                    "rule_ids_json, note) VALUES (?,?,?,?,?,?,?,?,?)",
                    (app["application_id"], now_iso(), actor, actor_id, action, app["credit_state"], target,
                     json.dumps(rule_ids or []), note))
        self.db.audit(actor, actor_id, "CREDIT_STATE_CHANGED", app["transaction_id"], app["application_id"],
                      app["credit_state"], target, rule_ids, {"action": action, "note": note})
        return self._app(app["application_id"])

    # ------------------------------------------------------------ notifications
    def notify(self, customer_id: str, kind: str, title: str, body: str, link: Optional[str] = None,
               dedupe_key: Optional[str] = None) -> None:
        self.db.run("INSERT OR IGNORE INTO notifications(customer_id, ts, business_date, kind, title, body, link, dedupe_key) "
                    "VALUES (?,?,?,?,?,?,?,?)", (customer_id, now_iso(), self.as_of.isoformat(), kind, title, body, link,
                                                 dedupe_key))

    # ------------------------------------------------------------ documents (simulated upload)
    def _documents(self, owner_type: str, owner_id: str, include_replaced: bool = False) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM documents WHERE owner_type = ? AND owner_id = ?"
        if not include_replaced:
            sql += " AND status != 'REPLACED'"
        return self.db.all(sql + " ORDER BY uploaded_at, rowid", (owner_type, owner_id))

    def _add_document(self, owner_type: str, owner_id: str, customer_id: str, doc: Dict[str, Any]) -> Dict[str, Any]:
        types = self.policy["documents"]["types"]
        dtype = doc.get("doc_type")
        if dtype not in types:
            raise ServiceError(422, "INVALID_DOCUMENT", "Unknown document type. Choose one of: %s" % ", ".join(types))
        name = str(doc.get("file_name") or "").strip()[:120]
        if not name:
            raise ServiceError(422, "FILE_REQUIRED", "Choose a file to upload.")
        size = doc.get("size_bytes") or 0
        if not isinstance(size, int) or size < 0 or size > self.policy["documents"]["max_file_mb"] * 1024 * 1024:
            raise ServiceError(422, "FILE_TOO_LARGE", "Files must be at most %s MB." % self.policy["documents"]["max_file_mb"])
        doc_id = self._new_id("DOC", "doc")
        prev = self.db.all("SELECT doc_id FROM documents WHERE owner_type = ? AND owner_id = ? AND doc_type = ? "
                           "AND status IN ('UPLOADED', 'SUBMITTED', 'MORE_REQUESTED')", (owner_type, owner_id, dtype))
        self.db.run("INSERT INTO documents(doc_id, owner_type, owner_id, customer_id, doc_type, file_name, mime_type, "
                    "size_bytes, status, uploaded_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (doc_id, owner_type, owner_id, customer_id, dtype, name, str(doc.get("mime_type") or "")[:80], size,
                     "UPLOADED", now_iso()))
        for p in prev:  # a new upload of the same type replaces the earlier one
            self.db.run("UPDATE documents SET status = 'REPLACED', replaced_by = ? WHERE doc_id = ?", (doc_id, p["doc_id"]))
        self.db.audit("CUSTOMER", customer_id, "DOCUMENT_UPLOADED",
                      owner_id if owner_type == "TRANSACTION" else None,
                      owner_id if owner_type == "APPLICATION" else None,
                      details={"owner": owner_type + ":" + owner_id, "doc_id": doc_id, "doc_type": dtype,
                               "file_name": name, "size_bytes": size, "replaced": [p["doc_id"] for p in prev],
                               "note": "Simulated upload - file content not transmitted or stored."})
        return self.db.one("SELECT * FROM documents WHERE doc_id = ?", (doc_id,))

    # ------------------------------------------------------------ checkout expiry
    def sweep_expired(self) -> int:
        """Expire unfinished customer-side checkouts whose 5-minute window passed.
        Never touches cases waiting for a credit officer."""
        now = self.clock()
        states = sorted(S.TX_EXPIRABLE)
        rows = self.db.all("SELECT transaction_id, expires_at FROM transactions WHERE expires_at IS NOT NULL AND state IN (%s)"
                           % ",".join("?" * len(states)), states)
        due = [r["transaction_id"] for r in rows if datetime.fromisoformat(r["expires_at"]) <= now]
        n = 0
        for tx_id in due:
            with self._unit():
                tx = self._tx(tx_id)
                if tx["state"] not in S.TX_EXPIRABLE or not tx["expires_at"] or datetime.fromisoformat(tx["expires_at"]) > now:
                    continue
                self._move_tx(tx, "EXPIRED", "SYSTEM", "checkout-timer", {"closure_reason": "CHECKOUT_EXPIRED"},
                              {"timeout_seconds": self.policy["checkout"]["timeout_seconds"]}, "CHECKOUT_EXPIRED")
                name = self.db.one("SELECT name FROM merchants WHERE merchant_id = ?", (tx["merchant_id"],))["name"]
                self.notify(tx["customer_id"], "STATUS", "Checkout expired",
                            "Your unfinished purchase at %s expired after 5 minutes. Nothing was charged. Scan the QR again "
                            "to restart." % name, "#/tx/" + tx_id, "expired:" + tx_id)
                n += 1
        return n

    def expire_now(self, tx_id: str, actor: str) -> Dict[str, Any]:
        """Demo control: move the deadline into the past and run the sweeper."""
        with self._unit():
            tx = self._tx(tx_id)
            if tx["state"] not in S.TX_EXPIRABLE:
                raise ServiceError(409, "NOT_EXPIRABLE", "Only unfinished customer-side checkouts can expire (state %s)."
                                   % tx["state"])
            self.db.run("UPDATE transactions SET expires_at = ? WHERE transaction_id = ?",
                        ((self.clock() - timedelta(seconds=1)).isoformat(timespec="seconds"), tx_id))
            self.db.audit("DEMO_CONTROL", actor, "DEMO_EXPIRE_NOW", tx_id)
        self.sweep_expired()
        return {"transaction_id": tx_id, "state": self._tx(tx_id)["state"]}
