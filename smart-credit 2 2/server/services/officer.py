"""Credit-officer workbench: application review queue and decisions."""
import json
from typing import Any, Dict, List, Optional

from ..store.db import now_iso
from .base import APPLICANT_FIELDS, ServiceError, _j


class OfficerMixin:
    def _profile_panel(self, customer_id: str) -> Dict[str, Any]:
        """Credit profile, risk indicators and repayment history for the officer."""
        cust = self._customer(customer_id)
        applicant = {k: v for k, v in _j(cust["applicant_json"], {}).items() if k in APPLICANT_FIELDS}
        applicant["internal_delinquency_flag"] = cust["internal_delinquency_flag"]
        ops = {"eq": lambda a, b: a == b, "lt": lambda a, b: a < b, "gt": lambda a, b: a > b}
        indicators = []
        for rr in self.policy["review_rules"]:
            v = applicant.get(rr["field"])
            hit = isinstance(v, (int, float)) and ops[rr["op"]](v, rr["value"])
            indicators.append({"rule_id": rr["rule_id"], "field": rr["field"], "value": v, "op": rr["op"],
                               "threshold": rr["value"], "source": rr["source"], "triggered": hit})
        self.refresh_servicing(customer_id)
        loans = [self._loan_view(l) for l in self.db.all("SELECT * FROM loans WHERE customer_id = ? ORDER BY created_at",
                                                          (customer_id,))]
        history = {"loans": [{k: l[k] for k in ("loan_ref", "merchant_name", "status", "principal_vnd", "outstanding_vnd",
                                                "paid_count", "instalment_count", "overdue_count")} for l in loans],
                   "overdue_instalments": sum(l["overdue_count"] for l in loans),
                   "repayments": sum(len(l["repayments"]) for l in loans),
                   "internal_delinquency_flag": cust["internal_delinquency_flag"],
                   "note": "Internal simulated history only - nothing is read from or reported to CIC."}
        return {"applicant": applicant, "risk_indicators": indicators, "repayment_history": history,
                "credit": self.credit_summary(customer_id),
                "customer": {"customer_id": cust["customer_id"], "display_name": cust["display_name"],
                             "derived_from": cust["derived_from"], "fixture_adjustment": cust["fixture_adjustment"],
                             "persona_note": cust["persona_note"]}}

    def officer_cases(self) -> Dict[str, List[Dict[str, Any]]]:
        self.sweep_expired()
        rows = self.db.all("SELECT a.*, t.state AS tx_state, t.amount_vnd, t.qr_ref, t.closure_reason, m.name AS merchant_name, "
                           "c.display_name FROM applications a JOIN transactions t USING(transaction_id) "
                           "JOIN merchants m ON m.merchant_id = t.merchant_id JOIN customers c ON c.customer_id = a.customer_id "
                           "ORDER BY a.updated_at DESC, a.rowid DESC")
        groups: Dict[str, List[Dict[str, Any]]] = {"pending_review": [], "awaiting_customer": [], "decided": []}
        for r in rows:
            routing_res = _j(r["routing_json"], {}) or {}
            docs = self.db.one("SELECT COUNT(*) n FROM documents WHERE owner_type = 'APPLICATION' AND owner_id = ? "
                               "AND status IN ('SUBMITTED', 'ACCEPTED')", (r["application_id"],))["n"]
            item = {"application_id": r["application_id"], "transaction_id": r["transaction_id"],
                    "customer_id": r["customer_id"], "customer_name": r["display_name"], "merchant_name": r["merchant_name"],
                    "amount_vnd": r["amount_vnd"], "credit_state": r["credit_state"], "readiness_status": r["readiness_status"],
                    "transaction_state": r["tx_state"], "closure_reason": r["closure_reason"], "documents": docs,
                    "rule_ids": routing_res.get("rule_ids", []), "updated_at": r["updated_at"], "version": r["version"]}
            if r["credit_state"] == "MANUAL_REVIEW" and r["tx_state"] == "PENDING_REVIEW":
                groups["pending_review"].append(item)
            elif r["credit_state"] == "MORE_INFORMATION_REQUIRED" and r["tx_state"] == "PENDING_REVIEW":
                groups["awaiting_customer"].append(item)
            else:
                groups["decided"].append(item)
        return groups

    def officer_case(self, app_id: str) -> Dict[str, Any]:
        self.sweep_expired()
        app = self._app(app_id)
        tx = self._tx(app["transaction_id"])
        merchant = self.db.one("SELECT * FROM merchants WHERE merchant_id = ?", (tx["merchant_id"],))
        consents = self.db.all("SELECT * FROM consents WHERE application_id = ? ORDER BY rowid", (app_id,))
        for c in consents:
            c["categories"] = _j(c.pop("categories_json"), [])
        decisions = self.db.all("SELECT * FROM decisions WHERE application_id = ? ORDER BY decision_id", (app_id,))
        for d in decisions:
            d["rule_ids"] = _j(d.pop("rule_ids_json"), [])
        profile = self._profile_panel(app["customer_id"])
        return {
            "application": {k: app[k] for k in ("application_id", "transaction_id", "customer_id", "requested_loan_vnd",
                                                 "tenure_months", "credit_state", "readiness_status", "decided_by",
                                                 "assessment_count", "version", "created_at", "updated_at",
                                                 "terms_confirmed_at")},
            "can_decide": app["credit_state"] == "MANUAL_REVIEW" and tx["state"] == "PENDING_REVIEW",
            "customer": dict(profile["customer"], indicative_limit_vnd=profile["credit"]["limit_vnd"]),
            "profile": profile,
            "transaction": {k: tx[k] for k in ("transaction_id", "qr_ref", "amount_vnd", "description", "method", "state",
                                                "closure_reason", "payment_ref", "tc_accepted_at", "tc_version", "created_at",
                                                "updated_at")},
            "merchant": merchant,
            "readiness": _j(app["readiness_json"]),
            "routing": _j(app["routing_json"]),
            "inputs": _j(app["input_snapshot_json"]),
            "info_request": _j(app["info_request_json"]),
            "info_submitted": _j(app["info_submitted_json"]),
            "documents": self._documents("APPLICATION", app_id, include_replaced=True),
            "terms": _j(app["terms_json"]),
            "consents": consents,
            "decisions": decisions,
            "audit": self.audit_log(transaction_id=tx["transaction_id"]),
        }

    def officer_decide(self, app_id: str, officer_id: str, action: str, note: Optional[str],
                       expected_version: Optional[int], requested_documents: Optional[List[str]] = None) -> Dict[str, Any]:
        if action not in ("approve", "reject", "request_info"):
            raise ServiceError(422, "INVALID_ACTION", "Action must be approve, reject or request_info.")
        if expected_version is None:
            raise ServiceError(422, "VERSION_REQUIRED", "expected_version is required to prevent stale decisions.")
        if action in ("reject", "request_info") and not (note or "").strip():
            raise ServiceError(422, "NOTE_REQUIRED", "A note is required for this action.")
        self.sweep_expired()
        with self._unit():
            app = self._app(app_id)
            if app["version"] != expected_version:
                raise ServiceError(409, "STALE_DECISION", "This case changed since you opened it. Reload before deciding.",
                                   {"current_version": app["version"], "credit_state": app["credit_state"]})
            if app["credit_state"] != "MANUAL_REVIEW":
                raise ServiceError(409, "INVALID_CREDIT_STATE", "Only cases in MANUAL_REVIEW can be decided.",
                                   {"current_state": app["credit_state"]})
            tx = self._tx(app["transaction_id"])
            if tx["state"] != "PENDING_REVIEW":
                raise ServiceError(409, "INVALID_TRANSACTION_STATE",
                                   "The customer's transaction is no longer pending review (%s)." % tx["state"],
                                   {"current_state": tx["state"]})
            self.db.audit("OFFICER", officer_id, "OFFICER_ACTION", tx["transaction_id"], app_id,
                          details={"action": action, "note": note, "auth": "DEMO-ONLY access code"})
            docs = [d for d in self._documents("APPLICATION", app_id) if d["status"] == "SUBMITTED"]
            link = "#/tx/" + tx["transaction_id"]
            if action == "approve":
                app = self._move_credit(app, "APPROVED", "OFFICER", officer_id, "OFFICER_APPROVE", note=note,
                                        extra={"decided_by": "OFFICER:" + officer_id}, expected_version=expected_version)
                for d in docs:
                    self.db.run("UPDATE documents SET status = 'ACCEPTED', review_note = ? WHERE doc_id = ?", (note, d["doc_id"]))
                self._set_terms(app, app["tenure_months"])
                self._move_tx(tx, "TERMS_PENDING_CONFIRMATION", "OFFICER", officer_id,
                              details={"timer": "new 5-minute window for customer confirmation"})
                self.notify(app["customer_id"], "STATUS", "Pay Later approved by a credit officer",
                            "Confirm the repayment terms within 5 minutes to complete your purchase.", link,
                            "officer-approve:" + app_id)
            elif action == "reject":
                self._move_credit(app, "REJECTED", "OFFICER", officer_id, "OFFICER_REJECT", note=note,
                                  extra={"decided_by": "OFFICER:" + officer_id}, expected_version=expected_version)
                for d in docs:
                    self.db.run("UPDATE documents SET status = 'REVIEWED', review_note = ? WHERE doc_id = ?", (note, d["doc_id"]))
                self._move_tx(tx, "CANCELLED", "OFFICER", officer_id, {"closure_reason": "CREDIT_REJECTED"},
                              {"disbursement": "none"})
                self.notify(app["customer_id"], "STATUS", "Pay Later not approved",
                            "A credit officer could not approve this request. Nothing was charged.", link,
                            "officer-reject:" + app_id)
            else:
                types = self.policy["documents"]["types"]
                wanted = requested_documents or ["payslip"]
                bad = [d for d in wanted if d not in types]
                if bad:
                    raise ServiceError(422, "INVALID_DOCUMENT", "Unknown document type: %s" % ", ".join(bad))
                for d in docs:
                    if d["doc_type"] in wanted:
                        self.db.run("UPDATE documents SET status = 'MORE_REQUESTED', review_note = ? WHERE doc_id = ?",
                                    (note, d["doc_id"]))
                req = {"requested_by": "OFFICER:" + officer_id, "ts": now_iso(), "note": note, "route": "OFFICER_REQUEST",
                       "items": [{"type": "document", "document": d, "label": types[d]} for d in wanted]}
                self._move_credit(app, "MORE_INFORMATION_REQUIRED", "OFFICER", officer_id, "OFFICER_REQUEST_INFO",
                                  note=note, extra={"info_request_json": json.dumps(req)}, expected_version=expected_version)
                self.db.audit("OFFICER", officer_id, "ADDITIONAL_DOCUMENTS_REQUESTED", tx["transaction_id"], app_id,
                              details={"documents": wanted, "note": note})
                self.notify(app["customer_id"], "ACTION", "More documents requested", note or "", link,
                            "officer-info:%s:%s" % (app_id, app["version"]))
        return self.officer_case(app_id)

    def set_demo_control(self, key: str, value: str, actor: str) -> Dict[str, Any]:
        if key != "force_next_payment_failure" or value not in ("0", "1"):
            raise ServiceError(422, "INVALID_CONTROL", "Unknown demo control.")
        with self._unit():
            self.db.set_control(key, value)
            self.db.audit("DEMO_CONTROL", actor, "DEMO_CONTROL_SET", details={key: value})
        return {key: value}
