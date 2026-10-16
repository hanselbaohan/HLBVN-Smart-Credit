"""SQLite persistence (stdlib sqlite3). Repository functions only - no
business rules live here.
"""
import hashlib
import hmac
import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS customers (
  customer_id TEXT PRIMARY KEY, display_name TEXT, initials TEXT, account_masked TEXT,
  balance_vnd INTEGER NOT NULL, indicative_limit_vnd INTEGER, derived_from TEXT,
  fixture_adjustment TEXT, persona_note TEXT, applicant_json TEXT NOT NULL, sources_json TEXT NOT NULL,
  extra_sources_json TEXT NOT NULL DEFAULT '{}', applicant_updates_json TEXT NOT NULL DEFAULT '{}',
  third_party_json TEXT NOT NULL DEFAULT '{}', internal_delinquency_flag INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS merchants (
  merchant_id TEXT PRIMARY KEY, name TEXT, category TEXT, city TEXT, pay_later_eligible INTEGER, icon TEXT
);
CREATE TABLE IF NOT EXISTS qr_codes (
  qr_ref TEXT PRIMARY KEY, merchant_id TEXT REFERENCES merchants, amount_vnd INTEGER NOT NULL,
  description TEXT, payload TEXT NOT NULL UNIQUE, simulated_failures INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS transactions (
  transaction_id TEXT PRIMARY KEY, qr_ref TEXT NOT NULL REFERENCES qr_codes, customer_id TEXT REFERENCES customers,
  merchant_id TEXT, amount_vnd INTEGER NOT NULL, description TEXT, method TEXT, tenure_months INTEGER,
  state TEXT NOT NULL, closure_reason TEXT, payment_ref TEXT, version INTEGER NOT NULL DEFAULT 1,
  tc_accepted_at TEXT, tc_version TEXT, expires_at TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
-- At most one non-terminal transaction per QR (prevents parallel duplicates).
CREATE UNIQUE INDEX IF NOT EXISTS ux_tx_active_qr ON transactions(qr_ref)
  WHERE state NOT IN ('CANCELLED', 'EXPIRED');
CREATE TABLE IF NOT EXISTS applications (
  application_id TEXT PRIMARY KEY, transaction_id TEXT NOT NULL REFERENCES transactions, customer_id TEXT,
  requested_loan_vnd INTEGER, tenure_months INTEGER, credit_state TEXT NOT NULL, readiness_status TEXT,
  readiness_json TEXT, routing_json TEXT, input_snapshot_json TEXT, info_request_json TEXT, info_submitted_json TEXT,
  terms_json TEXT, terms_confirmed_at TEXT, decided_by TEXT, assessment_count INTEGER NOT NULL DEFAULT 0,
  version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consents (
  consent_id TEXT PRIMARY KEY, application_id TEXT REFERENCES applications, customer_id TEXT,
  granted INTEGER NOT NULL, categories_json TEXT NOT NULL, policy_version TEXT, recorded_at TEXT NOT NULL,
  withdrawn_at TEXT, kind TEXT NOT NULL DEFAULT 'TC_INTERNAL', purpose TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
  decision_id INTEGER PRIMARY KEY AUTOINCREMENT, application_id TEXT REFERENCES applications, ts TEXT NOT NULL,
  actor_type TEXT, actor_id TEXT, action TEXT, from_state TEXT, to_state TEXT, rule_ids_json TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS payments (
  payment_id TEXT PRIMARY KEY, transaction_id TEXT NOT NULL REFERENCES transactions, qr_ref TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE, attempt_no INTEGER NOT NULL, method TEXT, amount_vnd INTEGER,
  status TEXT NOT NULL, failure_code TEXT, failure_message TEXT, created_at TEXT NOT NULL, completed_at TEXT
);
-- Database-level guarantee: one successful payment per QR transaction reference.
CREATE UNIQUE INDEX IF NOT EXISTS ux_payment_success_qr ON payments(qr_ref) WHERE status = 'SUCCEEDED';
CREATE TABLE IF NOT EXISTS loans (
  loan_ref TEXT PRIMARY KEY, application_id TEXT UNIQUE REFERENCES applications, transaction_id TEXT,
  customer_id TEXT, principal_vnd INTEGER, terms_json TEXT, created_at TEXT,
  merchant_name TEXT, status TEXT NOT NULL DEFAULT 'ACTIVE', closed_at TEXT
);
CREATE TABLE IF NOT EXISTS loan_instalments (
  loan_ref TEXT NOT NULL REFERENCES loans, number INTEGER NOT NULL, due_date TEXT NOT NULL,
  amount_vnd INTEGER NOT NULL, late_fee_vnd INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'SCHEDULED',
  paid_at TEXT, repayment_id TEXT, PRIMARY KEY (loan_ref, number)
);
CREATE TABLE IF NOT EXISTS loan_repayments (
  repayment_id TEXT PRIMARY KEY, loan_ref TEXT NOT NULL REFERENCES loans, customer_id TEXT,
  instalments_json TEXT NOT NULL, amount_vnd INTEGER NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL, failure_message TEXT, business_date TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
  doc_id TEXT PRIMARY KEY, owner_type TEXT NOT NULL, owner_id TEXT NOT NULL, customer_id TEXT NOT NULL,
  doc_type TEXT NOT NULL, file_name TEXT NOT NULL, mime_type TEXT, size_bytes INTEGER,
  status TEXT NOT NULL, uploaded_at TEXT NOT NULL, submitted_at TEXT, replaced_by TEXT, review_note TEXT
);
CREATE TABLE IF NOT EXISTS notifications (
  notification_id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT NOT NULL, ts TEXT NOT NULL,
  business_date TEXT, kind TEXT NOT NULL, title TEXT NOT NULL, body TEXT, link TEXT, dedupe_key TEXT UNIQUE,
  read_at TEXT
);
CREATE TABLE IF NOT EXISTS limit_requests (
  request_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, current_limit_vnd INTEGER, outstanding_vnd INTEGER,
  requested_limit_vnd INTEGER NOT NULL, approved_limit_vnd INTEGER, reason TEXT, linked_transaction_id TEXT,
  status TEXT NOT NULL, info_request_json TEXT, decided_by TEXT, decision_note TEXT,
  version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor_type TEXT NOT NULL, actor_id TEXT,
  event_type TEXT NOT NULL, transaction_id TEXT, application_id TEXT, from_state TEXT, to_state TEXT,
  rule_ids_json TEXT, details_json TEXT
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_events
  BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_events
  BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
CREATE TABLE IF NOT EXISTS demo_controls (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
"""

QR_SIGNING_KEY = b"demo-only-qr-signing-key"  # simulated merchant QR signature - NOT a real secret


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def qr_signature(qr_ref: str, merchant_id: str, amount: int) -> str:
    msg = ("%s|%s|%d" % (qr_ref, merchant_id, amount)).encode()
    return hmac.new(QR_SIGNING_KEY, msg, hashlib.sha256).hexdigest()[:12]


def qr_payload(qr_ref: str, merchant_id: str, amount: int) -> str:
    return "HLBQR|v1|%s|%s|%d|%s" % (qr_ref, merchant_id, amount, qr_signature(qr_ref, merchant_id, amount))


class Database:
    """Thin wrapper; one connection guarded by a re-entrant lock."""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # -- transactions -------------------------------------------------
    def begin(self) -> None:
        self.lock.acquire()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
        except Exception:
            self.lock.release()
            raise

    def commit(self) -> None:
        try:
            self.conn.execute("COMMIT")
        finally:
            self.lock.release()

    def rollback(self) -> None:
        try:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
        finally:
            self.lock.release()

    def close(self) -> None:
        self.conn.close()

    # -- generic helpers ----------------------------------------------
    # All access goes through the lock: one shared connection, serialised.
    def one(self, sql: str, args: Iterable[Any] = ()) -> Optional[Dict[str, Any]]:
        with self.lock:
            row = self.conn.execute(sql, tuple(args)).fetchone()
            return dict(row) if row else None

    def all(self, sql: str, args: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, tuple(args)).fetchall()]

    def run(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, tuple(args))

    def wipe(self) -> None:
        """Demo reset: drop and recreate every table in place (never deletes the file)."""
        with self.lock:
            names = [r["name"] for r in self.all("SELECT name FROM sqlite_master WHERE type = 'table' "
                                                 "AND name NOT LIKE 'sqlite_%'")]
            self.conn.execute("PRAGMA foreign_keys = OFF")
            for n in names:
                self.conn.execute('DROP TABLE IF EXISTS "%s"' % n)
            self.conn.executescript(SCHEMA)

    def next_id(self, name: str) -> int:
        self.run("INSERT INTO counters(name, value) VALUES (?, 1) ON CONFLICT(name) DO UPDATE SET value = value + 1", (name,))
        return self.one("SELECT value FROM counters WHERE name = ?", (name,))["value"]

    def update(self, table: str, key: str, key_value: Any, fields: Dict[str, Any],
               expected_version: Optional[int] = None) -> int:
        """Optimistic-concurrency update. Returns rows changed."""
        sets = ", ".join("%s = ?" % k for k in fields) + ", version = version + 1, updated_at = ?"
        args = list(fields.values()) + [now_iso(), key_value]
        sql = "UPDATE %s SET %s WHERE %s = ?" % (table, sets, key)
        if expected_version is not None:
            sql += " AND version = ?"
            args.append(expected_version)
        return self.run(sql, args).rowcount

    def audit(self, actor_type: str, actor_id: Optional[str], event_type: str, transaction_id: Optional[str] = None,
              application_id: Optional[str] = None, from_state: Optional[str] = None, to_state: Optional[str] = None,
              rule_ids: Optional[List[str]] = None, details: Optional[Dict[str, Any]] = None) -> None:
        self.run("INSERT INTO audit_events(ts, actor_type, actor_id, event_type, transaction_id, application_id, "
                 "from_state, to_state, rule_ids_json, details_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (now_iso(), actor_type, actor_id, event_type, transaction_id, application_id, from_state, to_state,
                  json.dumps(rule_ids or []), json.dumps(details or {}, ensure_ascii=False)))

    def control(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.one("SELECT value FROM demo_controls WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_control(self, key: str, value: str) -> None:
        self.run("INSERT INTO demo_controls(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, value))


def seed(db: Database, fixtures: Dict[str, Any]) -> None:
    with db.lock:
        db.begin()
        try:
            for c in fixtures["customers"]:
                db.run("INSERT INTO customers(customer_id, display_name, initials, account_masked, balance_vnd, "
                       "indicative_limit_vnd, derived_from, fixture_adjustment, persona_note, applicant_json, sources_json, "
                       "third_party_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (c["customer_id"], c["display_name"], c["initials"], c["account_masked"], c["balance_vnd"],
                        c["indicative_limit_vnd"], c["derived_from"], c["fixture_adjustment"], c["persona_note"],
                        json.dumps(c["applicant"]), json.dumps(c["sources"]), json.dumps(c.get("third_party") or {})))
            for m in fixtures["merchants"]:
                db.run("INSERT INTO merchants VALUES (?,?,?,?,?,?)",
                       (m["merchant_id"], m["name"], m["category"], m["city"], 1 if m["pay_later_eligible"] else 0, m["icon"]))
            for q in fixtures["qr_codes"]:
                db.run("INSERT INTO qr_codes VALUES (?,?,?,?,?,?)",
                       (q["qr_ref"], q["merchant_id"], q["amount_vnd"], q["description"],
                        qr_payload(q["qr_ref"], q["merchant_id"], q["amount_vnd"]), q["simulated_failures"]))
            db.audit("SYSTEM", "seed", "DEMO_DATA_SEEDED", details={"customers": len(fixtures["customers"]),
                                                                     "qr_codes": len(fixtures["qr_codes"])})
            db.commit()
        except Exception:
            db.rollback()
            raise


def open_database(path: str, fixtures: Dict[str, Any]) -> Database:
    db = Database(path)
    if db.one("SELECT COUNT(*) AS n FROM customers")["n"] == 0:
        seed(db, fixtures)
    return db
