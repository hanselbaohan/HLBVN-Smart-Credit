"""Customer checkout: QR, T&C, payment method, assessment, customer actions
(missing details, supporting documents, third-party consent, manual review),
terms and simulated payment."""
import json
import sqlite3
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..domain import states as S
from ..engine import readiness as R
from ..engine import routing, terms as T
from ..store.db import now_iso
from . import payment_gateway
from .base import ACCOUNT, APPLICANT_FIELDS, PAY_LATER, ServiceError, _j

CUSTOMER_PROVIDABLE_FIELDS = {"customer_profile"}


class CheckoutMixin:
    # ------------------------------------------------------------ catalogue
    def list_customers(self) -> List[Dict[str, Any]]:
        return [self.get_customer(c["customer_id"]) for c in self.db.all("SELECT customer_id FROM customers ORDER BY customer_id")]

    def get_customer(self, customer_id: str) -> Dict[str, Any]:
        c = self._customer(customer_id)
        out = {k: c[k] for k in ("customer_id", "display_name", "initials", "account_masked", "balance_vnd",
                                  "indicative_limit_vnd", "persona_note", "derived_from", "fixture_adjustment")}
        out["credit"] = self.credit_summary(customer_id)
        out["unread_notifications"] = self.db.one("SELECT COUNT(*) n FROM notifications WHERE customer_id = ? AND read_at IS NULL",
                                                  (customer_id,))["n"]
        return out

    def list_qr_codes(self) -> List[Dict[str, Any]]:
        rows = self.db.all("SELECT q.*, m.name AS merchant_name, m.category, m.icon, m.city, m.pay_later_eligible "
                           "FROM qr_codes q JOIN merchants m USING(merchant_id) ORDER BY q.qr_ref")
        for r in rows:
            paid = self.db.one("SELECT transaction_id FROM transactions WHERE qr_ref = ? AND state = 'PAYMENT_COMPLETED'",
                               (r["qr_ref"],))
            r["already_paid"] = bool(paid)
            r["pay_later_eligible"] = bool(r["pay_later_eligible"])
        return rows

    # ------------------------------------------------------------ 1. QR
    def resolve_qr(self, customer_id: str, payload: str) -> Dict[str, Any]:
        self.sweep_expired()
        self._customer(customer_id)
        parts = (payload or "").strip().split("|")
        if len(parts) != 6 or parts[0] != "HLBQR" or parts[1] != "v1":
            raise ServiceError(422, "QR_UNREADABLE", "This QR code isn't a supported (demo) merchant payment QR.")
        with self._unit():
            qr = self.db.one("SELECT * FROM qr_codes WHERE payload = ?", (payload.strip(),))
            if not qr:
                known = self.db.one("SELECT qr_ref FROM qr_codes WHERE qr_ref = ?", (parts[2],))
                self.db.audit("CUSTOMER", customer_id, "QR_REJECTED", details={"qr_ref": parts[2],
                              "reason": "signature/amount mismatch" if known else "unknown reference"})
                raise ServiceError(422, "QR_INVALID", "The QR details could not be verified with the merchant record. "
                                   "Nothing was charged.")
            existing = self.db.one("SELECT * FROM transactions WHERE qr_ref = ? AND state NOT IN ('CANCELLED', 'EXPIRED')",
                                   (qr["qr_ref"],))
            if existing:
                if existing["state"] == "PAYMENT_COMPLETED":
                    self.db.audit("CUSTOMER", customer_id, "DUPLICATE_QR_BLOCKED", existing["transaction_id"],
                                  details={"qr_ref": qr["qr_ref"]})
                    raise ServiceError(409, "QR_ALREADY_PAID", "This QR order has already been paid. No second payment "
                                       "will be made.", {"transaction_id": existing["transaction_id"]})
                if existing["customer_id"] != customer_id:
                    raise ServiceError(409, "QR_IN_USE", "This QR order is being paid by another customer.")
                self.db.audit("CUSTOMER", customer_id, "QR_TRANSACTION_RESUMED", existing["transaction_id"],
                              details={"qr_ref": qr["qr_ref"]})
                return {"resumed": True, "view": self._customer_view(existing["transaction_id"])}
            tx_id = self._new_id("TXN", "tx")
            ts = now_iso()
            self.db.run("INSERT INTO transactions(transaction_id, qr_ref, customer_id, merchant_id, amount_vnd, description, "
                        "state, expires_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (tx_id, qr["qr_ref"], customer_id, qr["merchant_id"], qr["amount_vnd"], qr["description"],
                         "QR_CREATED", self._deadline(), ts, ts))
            self.db.audit("CUSTOMER", customer_id, "QR_TRANSACTION_CREATED", tx_id, to_state="QR_CREATED",
                          details={"qr_ref": qr["qr_ref"], "merchant_id": qr["merchant_id"], "amount_vnd": qr["amount_vnd"],
                                   "amount_source": "server-side QR record (client amount not trusted)",
                                   "checkout_timeout_seconds": self.policy["checkout"]["timeout_seconds"]})
        return {"resumed": False, "view": self._customer_view(tx_id)}

    # ------------------------------------------------------------ 2. Terms & Conditions (single checkbox)
    def accept_terms_and_conditions(self, tx_id: str, customer_id: str, accepted: Any) -> Dict[str, Any]:
        if accepted is not True:
            raise ServiceError(422, "TC_NOT_ACCEPTED", "Please tick the box to agree to the Terms & Conditions and Privacy Policy.")
        self.sweep_expired()
        with self._unit():
            tx = self._tx(tx_id, customer_id)
            if tx["state"] != "QR_CREATED":
                raise ServiceError(409, "INVALID_TRANSACTION_STATE", "Terms can only be accepted right after scanning.",
                                   {"current_state": tx["state"]})
            if not tx["tc_accepted_at"]:
                version = self.policy["terms_and_conditions"]["version"]
                self.db.run("UPDATE transactions SET tc_accepted_at = ?, tc_version = ?, version = version + 1, updated_at = ? "
                            "WHERE transaction_id = ?", (now_iso(), version, now_iso(), tx_id))
                self.db.audit("CUSTOMER", customer_id, "TC_ACCEPTED", tx_id,
                              details={"tc_version": version, "covers": ["Terms & Conditions", "Privacy Policy",
                                                                         "personal-data collection, processing and protection"]})
        return self._customer_view(tx_id)

    # ------------------------------------------------------------ 3. payment method
    def select_payment_method(self, tx_id: str, customer_id: str, method: str,
                              tenure_months: Optional[int] = None) -> Dict[str, Any]:
        if method not in (PAY_LATER, ACCOUNT):
            raise ServiceError(422, "INVALID_METHOD", "Payment method must be PAY_LATER (Pay Later) or ACCOUNT (Pay Now).")
        self.sweep_expired()
        with self._unit():
            tx = self._tx(tx_id, customer_id)
            if tx["state"] not in ("QR_CREATED", "PAYMENT_METHOD_SELECTED"):
                raise ServiceError(409, "INVALID_TRANSACTION_STATE",
                                   "The payment method can't be changed now (state %s)." % tx["state"],
                                   {"current_state": tx["state"]})
            if not tx["tc_accepted_at"]:
                raise ServiceError(422, "TC_REQUIRED", "Please agree to the Terms & Conditions and Privacy Policy first.")
            if method == PAY_LATER:
                rng = self.policy["case_ranges"]["requested_loan_vnd"]
                if not (rng["min"] <= tx["amount_vnd"] <= rng["max"]):
                    below = tx["amount_vnd"] < rng["min"]
                    self.db.audit("CUSTOMER", customer_id, "PAY_LATER_BLOCKED_RANGE", tx_id,
                                  details={"amount_vnd": tx["amount_vnd"], "min": rng["min"], "max": rng["max"]})
                    raise ServiceError(422, "PAY_LATER_OUT_OF_RANGE",
                                       ("Pay Later is available from VND 1,600,000. This purchase is below the minimum, "
                                        "so please use Pay Now.") if below else
                                       ("Pay Later is available up to VND 90,100,000 per purchase. This purchase is above "
                                        "the maximum, so please use Pay Now or split the purchase."),
                                       {"min": rng["min"], "max": rng["max"], "amount_vnd": tx["amount_vnd"],
                                        "direction": "BELOW_MIN" if below else "ABOVE_MAX"})
                credit = self.credit_summary(customer_id)
                if tx["amount_vnd"] > credit["available_vnd"]:
                    self.db.audit("CUSTOMER", customer_id, "PAY_LATER_BLOCKED_LIMIT", tx_id,
                                  details={"amount_vnd": tx["amount_vnd"], "available_vnd": credit["available_vnd"]})
                    raise ServiceError(422, "CREDIT_LIMIT_EXCEEDED",
                                       "This purchase is above your available Pay Later credit. You can request a credit "
                                       "limit increase or pay now.",
                                       {"available_vnd": credit["available_vnd"], "limit_vnd": credit["limit_vnd"],
                                        "outstanding_vnd": credit["outstanding_vnd"], "amount_vnd": tx["amount_vnd"],
                                        "shortfall_vnd": tx["amount_vnd"] - credit["available_vnd"],
                                        "can_request_increase": credit["open_limit_request"] is None})
                tenure_months = tenure_months or self.policy["repayment_terms"]["default_tenure"]
                if tenure_months not in self.policy["repayment_terms"]["offered_tenures"]:
                    raise ServiceError(422, "INVALID_TENURE", "Tenure must be one of %s months."
                                       % self.policy["repayment_terms"]["offered_tenures"])
            else:
                tenure_months = None
            tx = self._move_tx(tx, "PAYMENT_METHOD_SELECTED", "CUSTOMER", customer_id,
                               {"method": method, "tenure_months": tenure_months},
                               {"method": method, "label": "Pay Later" if method == PAY_LATER else "Pay Now",
                                "tenure_months": tenure_months}, "PAYMENT_METHOD_SELECTED")
            if method == PAY_LATER:
                self._open_application(tx, customer_id)
        return self._customer_view(tx_id)

    def _open_application(self, tx: Dict[str, Any], customer_id: str) -> None:
        """Create the credit application. The T&C/Privacy acceptance is recorded
        as the basis for using INTERNAL data - no separate consent prompt."""
        app_id = self._new_id("APP", "app")
        ts = now_iso()
        self.db.run("INSERT INTO applications(application_id, transaction_id, customer_id, requested_loan_vnd, "
                    "tenure_months, credit_state, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                    (app_id, tx["transaction_id"], customer_id, tx["amount_vnd"], tx["tenure_months"], "ASSESSMENT_PENDING", ts, ts))
        self.db.audit("CUSTOMER", customer_id, "APPLICATION_CREATED", tx["transaction_id"], app_id, to_state="ASSESSMENT_PENDING",
                      details={"requested_loan_vnd": tx["amount_vnd"], "tenure_months": tx["tenure_months"]})
        cats = self.policy["terms_and_conditions"]["internal_data_categories"]
        self.db.run("INSERT INTO consents(consent_id, application_id, customer_id, granted, categories_json, policy_version, "
                    "recorded_at, kind, purpose) VALUES (?,?,?,?,?,?,?,?,?)",
                    (self._new_id("CNS", "consent"), app_id, customer_id, 1, json.dumps(cats), tx["tc_version"], ts,
                     "TC_INTERNAL", "Internal (simulated) bank data under accepted T&C/Privacy Policy " + str(tx["tc_version"])))
        self.db.audit("SYSTEM", "checkout", "INTERNAL_DATA_BASIS_RECORDED", tx["transaction_id"], app_id,
                      details={"categories": cats, "basis": tx["tc_version"], "separate_consent_prompt": False})
        self._move_tx(self._tx(tx["transaction_id"]), "CONSENT_COMPLETED", "SYSTEM", "checkout", event="READY_FOR_ASSESSMENT")

    def withdraw_consent(self, tx_id: str, customer_id: str) -> Dict[str, Any]:
        with self._unit():
            tx = self._tx(tx_id, customer_id)
            app = self._app_for_tx(tx_id)
            if not app or tx["state"] in S.TX_TERMINAL or tx["state"] == "DISBURSEMENT_PROCESSING":
                raise ServiceError(409, "CANNOT_WITHDRAW", "Consent can't be withdrawn at this stage.",
                                   {"current_state": tx["state"]})
            self.db.run("UPDATE consents SET withdrawn_at = ? WHERE application_id = ? AND withdrawn_at IS NULL",
                        (now_iso(), app["application_id"]))
            self.db.audit("CUSTOMER", customer_id, "CONSENT_WITHDRAWN", tx_id, app["application_id"])
            if app["credit_state"] in ("ASSESSMENT_PENDING", "MANUAL_REVIEW", "MORE_INFORMATION_REQUIRED"):
                self._move_credit(app, "POLICY_STOP", "DECISION_ENGINE", "routing", "HARD_STOP", ["HS-01"],
                                  "Data-use consent withdrawn by customer.",
                                  {"routing_json": json.dumps({"route": "POLICY_STOP", "rule_ids": ["HS-01"],
                                                               "customer_message": "You withdrew permission to use your "
                                                               "data, so Pay Later stopped. Nothing was charged."})})
            self._move_tx(tx, "CANCELLED", "CUSTOMER", customer_id, {"closure_reason": "CONSENT_WITHDRAWN"})
        return self._customer_view(tx_id)

    # ------------------------------------------------------------ 4/5. assessment & routing
    def _inputs(self, app: Dict[str, Any], tx: Dict[str, Any]) -> Dict[str, Any]:
        cust = self._customer(app["customer_id"])
        applicant = {k: v for k, v in _j(cust["applicant_json"], {}).items() if k in APPLICANT_FIELDS}
        applicant["internal_delinquency_flag"] = cust["internal_delinquency_flag"]
        sources = _j(cust["sources_json"], {})
        submitted = _j(app["info_submitted_json"], {}) or {}
        applicant.update(submitted.get("fields", {}))
        sources.update(submitted.get("sources", {}))
        cats = set()
        for c in self.db.all("SELECT * FROM consents WHERE application_id = ? AND granted = 1 AND withdrawn_at IS NULL",
                             (app["application_id"],)):
            cats |= set(_j(c["categories_json"], []))
        consent = {"granted": bool(cats), "categories": sorted(cats)}
        application = {"application_id": app["application_id"], "applicant_id": cust["customer_id"],
                       "qr_ref": tx["qr_ref"], "merchant_id": tx["merchant_id"],
                       "requested_loan_vnd": app["requested_loan_vnd"], "loan_tenure_months": app["tenure_months"]}
        merchant = self.db.one("SELECT * FROM merchants WHERE merchant_id = ?", (tx["merchant_id"],))
        credit = self.credit_summary(cust["customer_id"])
        return {"application": application, "applicant": applicant, "sources": sources, "consent": consent,
                "merchant": {"merchant_id": merchant["merchant_id"], "category": merchant["category"],
                             "pay_later_eligible": bool(merchant["pay_later_eligible"])},
                "credit": credit, "third_party_available": self._third_party_available(app, cust),
                "as_of": self.as_of.isoformat()}

    def _third_party_available(self, app: Dict[str, Any], cust: Dict[str, Any]) -> bool:
        if not _j(cust["third_party_json"], {}):
            return False
        asked = self.db.one("SELECT 1 FROM consents WHERE application_id = ? AND kind = 'THIRD_PARTY'", (app["application_id"],))
        return not asked

    def assess(self, app_id: str, customer_id: Optional[str] = None, actor: str = "CUSTOMER") -> Dict[str, Any]:
        self.sweep_expired()
        with self._unit():
            app = self._app(app_id, customer_id)
            if app["credit_state"] != "ASSESSMENT_PENDING":
                # Idempotent: a repeated request returns the existing result.
                return self._customer_view(app["transaction_id"], already_assessed=True)
            tx = self._tx(app["transaction_id"])
            if tx["state"] not in ("CONSENT_COMPLETED", "PENDING_REVIEW"):
                raise ServiceError(409, "INVALID_TRANSACTION_STATE", "Assessment can't start in state %s." % tx["state"],
                                   {"current_state": tx["state"]})
            tx = self._move_tx(tx, "ASSESSMENT_IN_PROGRESS", "DECISION_ENGINE", "engine", event="ASSESSMENT_STARTED",
                               details={"policy_version": self.policy["policy_version"],
                                        "assessment_no": app["assessment_count"] + 1})
            started = time.perf_counter()
            inp = self._inputs(app, tx)
            res = R.evaluate(inp["application"], inp["applicant"], inp["sources"], inp["consent"], self.policy, self.as_of)
            dec = routing.decide(res, {"merchant": inp["merchant"], "requested_loan_vnd": app["requested_loan_vnd"],
                                       "loan_tenure_months": app["tenure_months"],
                                       "available_credit_vnd": inp["credit"]["available_vnd"],
                                       "third_party_available": inp["third_party_available"]}, self.policy)
            dec["engine_ms"] = round((time.perf_counter() - started) * 1000, 2)
            self.db.audit("DECISION_ENGINE", "engine", "ASSESSMENT_COMPLETED", tx["transaction_id"], app_id,
                          details={"readiness_status": res["status"], "engine_ms": dec["engine_ms"],
                                   "checks": [(c["check_id"], c["result"]) for c in res["checks_performed"]]})
            warnings = res["validation_errors"] + res["verification_warnings"] + res["evidence_gaps"] + res["review_reasons"]
            if warnings:
                self.db.audit("DECISION_ENGINE", "engine", "VALIDATION_WARNINGS", tx["transaction_id"], app_id,
                              rule_ids=sorted({w["rule_id"] for w in warnings}),
                              details={"messages": [w["message"] for w in warnings]})
            self.db.audit("DECISION_ENGINE", "engine", "ROUTING_DECISION", tx["transaction_id"], app_id,
                          rule_ids=dec["rule_ids"], details={"route": dec["route"], "state_path": dec["state_path"]})
            extra = {"readiness_status": res["status"], "readiness_json": json.dumps(res), "routing_json": json.dumps(dec),
                     "input_snapshot_json": json.dumps(inp), "decided_by": dec["decided_by"],
                     "assessment_count": app["assessment_count"] + 1}
            if dec["final_credit_state"] == "MORE_INFORMATION_REQUIRED":
                extra["info_request_json"] = json.dumps({"requested_by": "DECISION_ENGINE", "ts": now_iso(),
                                                         "route": dec["route"], "items": dec.get("requested_items", []),
                                                         "note": dec["customer_message"]})
            for i, target in enumerate(dec["state_path"]):
                app = self._move_credit(app, target, dec["decided_by"] if target == "APPROVED" else "DECISION_ENGINE",
                                        "engine", "ENGINE_ROUTING", dec["rule_ids"],
                                        dec["reasons"][0]["message"] if dec["reasons"] else None, extra if i == 0 else None)
            final = dec["final_credit_state"]
            link = "#/tx/" + tx["transaction_id"]
            if final == "APPROVED":
                self._set_terms(app, app["tenure_months"])
                self._move_tx(tx, "TERMS_PENDING_CONFIRMATION", "DECISION_ENGINE", "engine")
                self.notify(app["customer_id"], "STATUS", "Pay Later approved", "Confirm the repayment terms within 5 minutes "
                            "to complete your purchase.", link, "approved:%s:%s" % (app_id, app["assessment_count"]))
            elif final in ("MANUAL_REVIEW", "MORE_INFORMATION_REQUIRED"):
                self._move_tx(tx, "PENDING_REVIEW", "DECISION_ENGINE", "engine",
                              details={"queue": "CREDIT_OFFICER" if final == "MANUAL_REVIEW" else "CUSTOMER_ACTION",
                                       "timer": "paused"})
                if final == "MANUAL_REVIEW":
                    self.db.audit("DECISION_ENGINE", "engine", "REVIEW_CASE_CREATED", tx["transaction_id"], app_id,
                                  rule_ids=dec["rule_ids"])
                    self.notify(app["customer_id"], "STATUS", "Additional review required",
                                "A credit officer is reviewing your Pay Later request. No money has been taken.", link,
                                "review:%s:%s" % (app_id, app["assessment_count"]))
                else:
                    self.notify(app["customer_id"], "ACTION", "Action needed for your Pay Later request",
                                dec["customer_message"], link, "action:%s:%s" % (app_id, app["assessment_count"]))
            elif final == "POLICY_STOP":
                self._move_tx(tx, "CANCELLED", "DECISION_ENGINE", "engine", {"closure_reason": "POLICY_STOP"},
                              {"rule_ids": dec["rule_ids"]})
        return self._customer_view(app["transaction_id"])

    # ------------------------------------------------------------ customer actions while MORE_INFORMATION_REQUIRED
    def _require_more_info(self, app_id: str, customer_id: str) -> Dict[str, Any]:
        app = self._app(app_id, customer_id)
        if app["credit_state"] != "MORE_INFORMATION_REQUIRED":
            raise ServiceError(409, "INVALID_CREDIT_STATE", "No information has been requested for this application.",
                               {"current_state": app["credit_state"]})
        tx = self._tx(app["transaction_id"])
        if tx["state"] != "PENDING_REVIEW":
            raise ServiceError(409, "INVALID_TRANSACTION_STATE", "This purchase is no longer waiting for information.",
                               {"current_state": tx["state"]})
        return app

    def _options(self, app: Dict[str, Any]) -> List[str]:
        req = _j(app["info_request_json"], {}) or {}
        opts: List[str] = []
        for i in req.get("items", []):
            if i.get("type") == "evidence_options":
                opts += i.get("options", [])
            if i.get("type") == "document" and "documents" not in opts:
                opts.append("documents")
        if req.get("third_party_declined") and "third_party_consent" in opts:
            opts.remove("third_party_consent")
        return list(dict.fromkeys(opts))

    def upload_application_document(self, app_id: str, customer_id: str, doc: Dict[str, Any]) -> Dict[str, Any]:
        with self._unit():
            app = self._require_more_info(app_id, customer_id)
            if "documents" not in self._options(app):
                raise ServiceError(409, "DOCUMENTS_NOT_REQUESTED", "Documents haven't been requested for this application.")
            self._add_document("APPLICATION", app_id, customer_id, doc)
        return self._customer_view(app["transaction_id"])

    def submit_information(self, app_id: str, customer_id: str, fields: Dict[str, Any],
                           documents: Optional[List[str]] = None) -> Dict[str, Any]:
        """Submit requested details and/or the uploaded documents, then resume the assessment.
        Documents never auto-approve: they stay unverified until a credit officer reviews them."""
        with self._unit():
            app = self._require_more_info(app_id, customer_id)
            fields = fields or {}
            bad = [f for f in fields if f not in CUSTOMER_PROVIDABLE_FIELDS]
            if bad:
                raise ServiceError(422, "FIELD_NOT_ACCEPTED", "These details can't be provided by the customer: %s" % ", ".join(bad))
            if "customer_profile" in fields and fields["customer_profile"] not in \
                    self.policy["case_ranges"]["customer_profiles"]["allowed"]:
                raise ServiceError(422, "INVALID_VALUE", "Please choose one of the listed profiles.")
            request = _j(app["info_request_json"], {}) or {}
            needed_fields = [i["field"] for i in request.get("items", []) if i.get("type") == "field"]
            missing = [f for f in needed_fields if f not in fields]
            new_docs = [d for d in self._documents("APPLICATION", app_id) if d["status"] == "UPLOADED"]
            doc_types = [i["document"] for i in request.get("items", []) if i.get("type") == "document"]
            if doc_types and not needed_fields:
                have = {d["doc_type"] for d in new_docs}
                missing += [self.policy["documents"]["types"][t] for t in doc_types if t not in have]
            if missing:
                raise ServiceError(422, "INFORMATION_INCOMPLETE", "Please provide: %s" % ", ".join(missing))
            submitted = _j(app["info_submitted_json"], {}) or {"fields": {}, "sources": {}, "documents": []}
            submitted["fields"].update(fields)
            tx = self._tx(app["transaction_id"])
            if new_docs:
                ts = now_iso()
                for d in new_docs:
                    self.db.run("UPDATE documents SET status = 'SUBMITTED', submitted_at = ? WHERE doc_id = ?", (ts, d["doc_id"]))
                all_docs = [d for d in self._documents("APPLICATION", app_id) if d["status"] in ("SUBMITTED", "ACCEPTED")]
                submitted["documents"] = sorted({d["doc_type"] for d in all_docs})
                submitted["sources"]["supporting_documents"] = {
                    "verified": False, "observed_at": self.as_of.isoformat(), "method": "CUSTOMER_UPLOAD_PENDING_OFFICER",
                    "documents": submitted["documents"]}
                if not self.db.one("SELECT 1 FROM consents WHERE application_id = ? AND kind = 'CUSTOMER_PROVIDED'", (app_id,)):
                    self.db.run("INSERT INTO consents(consent_id, application_id, customer_id, granted, categories_json, "
                                "policy_version, recorded_at, kind, purpose) VALUES (?,?,?,?,?,?,?,?,?)",
                                (self._new_id("CNS", "consent"), app_id, customer_id, 1, json.dumps(["supporting_documents"]),
                                 self.policy["policy_version"], ts, "CUSTOMER_PROVIDED", "Documents supplied by the customer"))
                self.db.audit("CUSTOMER", customer_id, "DOCUMENTS_SUBMITTED", tx["transaction_id"], app_id,
                              details={"documents": [{"doc_id": d["doc_id"], "type": d["doc_type"], "file": d["file_name"]}
                                                     for d in new_docs]})
            elif not fields:
                raise ServiceError(422, "INFORMATION_INCOMPLETE", "Nothing to submit yet.")
            if fields:
                self.db.audit("CUSTOMER", customer_id, "INFORMATION_SUBMITTED", tx["transaction_id"], app_id,
                              details={"fields": sorted(fields)})
            self._move_credit(app, "ASSESSMENT_PENDING", "CUSTOMER", customer_id, "INFORMATION_SUBMITTED",
                              extra={"info_submitted_json": json.dumps(submitted)})
        return self.assess(app_id, customer_id)

    def third_party_consent(self, app_id: str, customer_id: str, granted: Any) -> Dict[str, Any]:
        """Contextual consent for THIRD-PARTY data, asked only when evidence is insufficient.
        Declining never rejects - the customer can provide documents or ask for manual review."""
        with self._unit():
            app = self._require_more_info(app_id, customer_id)
            if "third_party_consent" not in self._options(app):
                raise ServiceError(409, "THIRD_PARTY_NOT_NEEDED", "Third-party data isn't needed for this application.")
            cust = self._customer(customer_id)
            tp = _j(cust["third_party_json"], {})
            cats = sorted(tp)
            self.db.run("INSERT INTO consents(consent_id, application_id, customer_id, granted, categories_json, policy_version, "
                        "recorded_at, kind, purpose) VALUES (?,?,?,?,?,?,?,?,?)",
                        (self._new_id("CNS", "consent"), app_id, customer_id, 1 if granted is True else 0, json.dumps(cats),
                         self.policy["policy_version"], now_iso(), "THIRD_PARTY",
                         "One-time check of simulated partner e-wallet activity for this Pay Later request"))
            self.db.audit("CUSTOMER", customer_id, "THIRD_PARTY_CONSENT_GRANTED" if granted is True else "THIRD_PARTY_CONSENT_DECLINED",
                          app["transaction_id"], app_id, details={"categories": cats, "provider": "Simulated partner API"})
            if granted is not True:
                req = _j(app["info_request_json"], {}) or {}
                req["third_party_declined"] = True
                req["note"] = ("No problem - your request is not rejected. You can upload supporting documents instead, "
                               "or ask a credit officer to review it manually.")
                self.db.run("UPDATE applications SET info_request_json = ?, version = version + 1 WHERE application_id = ?",
                            (json.dumps(req), app_id))
                return_view = True
            else:
                submitted = _j(app["info_submitted_json"], {}) or {"fields": {}, "sources": {}, "documents": []}
                for src_id, block in tp.items():
                    submitted["fields"].update(block.get("applicant", {}))
                    submitted["sources"][src_id] = block.get("source", {})
                self._move_credit(app, "ASSESSMENT_PENDING", "CUSTOMER", customer_id, "THIRD_PARTY_DATA_ADDED",
                                  extra={"info_submitted_json": json.dumps(submitted)})
                return_view = False
        return self._customer_view(app["transaction_id"]) if return_view else self.assess(app_id, customer_id)

    def request_manual_review(self, app_id: str, customer_id: str) -> Dict[str, Any]:
        with self._unit():
            app = self._require_more_info(app_id, customer_id)
            if "manual_review" not in self._options(app):
                raise ServiceError(409, "MANUAL_REVIEW_NOT_AVAILABLE", "Please provide the requested details instead.")
            rt = _j(app["routing_json"], {}) or {}
            rt.setdefault("reasons", []).append({"rule_id": "CUST-MR", "message": "Customer asked for manual review instead "
                                                 "of providing more data/documents."})
            rt["route"] = routing.ROUTE_REVIEW
            self._move_credit(app, "MANUAL_REVIEW", "CUSTOMER", customer_id, "CUSTOMER_REQUESTED_REVIEW", ["CUST-MR"],
                              extra={"routing_json": json.dumps(rt)})
            self.db.audit("CUSTOMER", customer_id, "REVIEW_CASE_CREATED", app["transaction_id"], app_id, rule_ids=["CUST-MR"])
        return self._customer_view(app["transaction_id"])

    # ------------------------------------------------------------ 7. terms
    def _set_terms(self, app: Dict[str, Any], tenure: int) -> Dict[str, Any]:
        terms = T.build_terms(app["requested_loan_vnd"], tenure, self.as_of, self.policy)
        self.db.run("UPDATE applications SET terms_json = ?, tenure_months = ?, terms_confirmed_at = NULL "
                    "WHERE application_id = ?", (json.dumps(terms), tenure, app["application_id"]))
        return terms

    def preview_terms(self, app_id: str, customer_id: str, tenure: int) -> Dict[str, Any]:
        app = self._app(app_id, customer_id)
        if app["credit_state"] != "APPROVED":
            raise ServiceError(409, "NOT_APPROVED", "Terms are only available for approved applications.")
        try:
            return T.build_terms(app["requested_loan_vnd"], tenure, self.as_of, self.policy)
        except ValueError as e:
            raise ServiceError(422, "INVALID_TENURE", str(e))

    def confirm_terms(self, app_id: str, customer_id: str, tenure: int, accepted: Any) -> Dict[str, Any]:
        if accepted is not True:
            raise ServiceError(422, "TERMS_NOT_ACCEPTED", "Please tick the box to confirm you accept the demo terms.")
        if tenure not in self.policy["repayment_terms"]["offered_tenures"]:
            raise ServiceError(422, "INVALID_TENURE", "Tenure must be one of the offered options.")
        self.sweep_expired()
        with self._unit():
            app = self._app(app_id, customer_id)
            tx = self._tx(app["transaction_id"])
            if app["credit_state"] != "APPROVED" or tx["state"] != "TERMS_PENDING_CONFIRMATION":
                raise ServiceError(409, "INVALID_STATE", "Terms can only be confirmed for an approved purchase awaiting "
                                   "confirmation.", {"credit_state": app["credit_state"], "transaction_state": tx["state"]})
            terms = self._set_terms(app, tenure)
            self.db.run("UPDATE applications SET terms_confirmed_at = ?, version = version + 1 WHERE application_id = ?",
                        (now_iso(), app_id))
            self.db.run("UPDATE transactions SET tenure_months = ?, version = version + 1 WHERE transaction_id = ?",
                        (tenure, tx["transaction_id"]))
            self.db.audit("CUSTOMER", customer_id, "TERMS_CONFIRMED", tx["transaction_id"], app_id,
                          details={"tenure_months": tenure, "monthly_instalment_vnd": terms["monthly_instalment_vnd"],
                                   "total_repayable_vnd": terms["total_repayable_vnd"], "terms_label": terms["label"]})
        return self._customer_view(app["transaction_id"])

    # ------------------------------------------------------------ 8. simulated payment
    def pay(self, tx_id: str, customer_id: str, idempotency_key: str) -> Dict[str, Any]:
        if not idempotency_key or len(idempotency_key) > 100:
            raise ServiceError(422, "IDEMPOTENCY_KEY_REQUIRED", "An Idempotency-Key header is required.")
        self.sweep_expired()
        # Phase 1 - validate and reserve.
        with self._unit():
            prior = self.db.one("SELECT * FROM payments WHERE idempotency_key = ?", (idempotency_key,))
            if prior:
                if prior["transaction_id"] != tx_id:
                    raise ServiceError(409, "IDEMPOTENCY_KEY_REUSED", "This idempotency key was used for another payment.")
                self.db.audit("CUSTOMER", customer_id, "DUPLICATE_PAYMENT_REQUEST", tx_id,
                              details={"payment_id": prior["payment_id"], "result": prior["status"]})
                view = self._customer_view(tx_id)
                view["idempotent_replay"] = True
                return view
            tx = self._tx(tx_id, customer_id)
            if tx["state"] == "PAYMENT_COMPLETED":
                self.db.audit("CUSTOMER", customer_id, "DUPLICATE_PAYMENT_BLOCKED", tx_id)
                raise ServiceError(409, "DUPLICATE_PAYMENT", "This purchase has already been paid. No second payment was made.")
            if tx["state"] == "DISBURSEMENT_PROCESSING":
                raise ServiceError(409, "PAYMENT_IN_PROGRESS", "This payment is already being processed.")
            if tx["state"] in ("CANCELLED", "EXPIRED"):
                raise ServiceError(409, "TRANSACTION_CLOSED", "This purchase was %s and can't be paid. Scan the QR again to "
                                   "restart." % tx["state"].lower(), {"current_state": tx["state"]})
            qr = self.db.one("SELECT * FROM qr_codes WHERE qr_ref = ?", (tx["qr_ref"],))
            if qr["amount_vnd"] != tx["amount_vnd"]:
                raise ServiceError(409, "AMOUNT_MISMATCH", "Amount does not match the merchant QR record.")
            if tx["method"] == PAY_LATER:
                app = self._app_for_tx(tx_id)
                if not app or app["credit_state"] != "APPROVED":
                    raise ServiceError(409, "NOT_APPROVED", "Pay Later has not been approved for this purchase.")
                if not app["terms_confirmed_at"]:
                    raise ServiceError(409, "TERMS_NOT_CONFIRMED", "Please confirm the repayment terms first.")
                if tx["state"] not in ("TERMS_PENDING_CONFIRMATION", "PAYMENT_FAILED"):
                    raise ServiceError(409, "INVALID_TRANSACTION_STATE", "Payment can't start in state %s." % tx["state"])
            elif tx["method"] == ACCOUNT:
                if tx["state"] not in ("PAYMENT_METHOD_SELECTED", "PAYMENT_FAILED"):
                    raise ServiceError(409, "INVALID_TRANSACTION_STATE", "Payment can't start in state %s." % tx["state"])
            else:
                raise ServiceError(409, "NO_METHOD", "Choose a payment method first.")
            attempts = self.db.one("SELECT COUNT(*) AS n FROM payments WHERE transaction_id = ?", (tx_id,))["n"]
            if attempts > self.policy["payment_simulation"]["max_retries"]:
                raise ServiceError(409, "RETRY_LIMIT", "Retry limit reached. Please cancel and try again later.")
            payment_id = self._new_id("PAY", "payment")
            self.db.run("INSERT INTO payments(payment_id, transaction_id, qr_ref, idempotency_key, attempt_no, method, "
                        "amount_vnd, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (payment_id, tx_id, tx["qr_ref"], idempotency_key, attempts + 1, tx["method"], tx["amount_vnd"],
                         "PROCESSING", now_iso()))
            self._move_tx(tx, "DISBURSEMENT_PROCESSING", "CUSTOMER", customer_id, event="PAYMENT_PROCESSING_STARTED",
                          details={"payment_id": payment_id, "attempt_no": attempts + 1, "simulated": True,
                                   "operation": "SIMULATED_DISBURSEMENT_TO_MERCHANT" if tx["method"] == PAY_LATER
                                   else "SIMULATED_ACCOUNT_DEBIT"})
            force = self.db.control("force_next_payment_failure") == "1"
            if force:
                self.db.set_control("force_next_payment_failure", "0")
        # Phase 2 - simulated network latency, outside the lock.
        if self.delay_ms:
            time.sleep(self.delay_ms / 1000.0)
        # Phase 3 - settle.
        with self._unit():
            tx = self._tx(tx_id)
            cust = self._customer(customer_id)
            result = payment_gateway.process(tx["method"], tx["amount_vnd"], attempts + 1, qr, cust["balance_vnd"], force)
            if result["ok"]:
                try:
                    self.db.run("UPDATE payments SET status = 'SUCCEEDED', completed_at = ? WHERE payment_id = ?",
                                (now_iso(), payment_id))
                except sqlite3.IntegrityError:
                    result = {"ok": False, "code": "DUPLICATE_BLOCKED", "message": "A payment for this QR already succeeded."}
            merchant = self.db.one("SELECT name FROM merchants WHERE merchant_id = ?", (tx["merchant_id"],))["name"]
            if result["ok"]:
                if tx["method"] == ACCOUNT:
                    self.db.run("UPDATE customers SET balance_vnd = balance_vnd - ? WHERE customer_id = ?",
                                (tx["amount_vnd"], customer_id))
                else:
                    self._book_loan(self._app_for_tx(tx_id), tx, merchant)
                self._move_tx(tx, "PAYMENT_COMPLETED", "SYSTEM", "payment-simulator", {"payment_ref": payment_id},
                              {"payment_id": payment_id, "result": result}, "PAYMENT_COMPLETED")
                self.notify(customer_id, "STATUS", "Payment completed", "%s paid to %s (simulated)."
                            % ("{:,}".format(tx["amount_vnd"]), merchant), "#/tx/" + tx_id, "paid:" + tx_id)
            else:
                self.db.run("UPDATE payments SET status = 'FAILED', failure_code = ?, failure_message = ?, completed_at = ? "
                            "WHERE payment_id = ?", (result["code"], result["message"], now_iso(), payment_id))
                self._move_tx(tx, "PAYMENT_FAILED", "SYSTEM", "payment-simulator", {"expires_at": self._deadline()},
                              {"payment_id": payment_id, "result": result}, "PAYMENT_FAILED")
                self.notify(customer_id, "STATUS", "Payment failed", result["message"], "#/tx/" + tx_id,
                            "failed:" + payment_id)
        return self._customer_view(tx_id)

    def cancel(self, tx_id: str, customer_id: str) -> Dict[str, Any]:
        self.sweep_expired()
        with self._unit():
            tx = self._tx(tx_id, customer_id)
            if tx["state"] not in S.TX_ACTIVE_BEFORE_PAYMENT:
                raise ServiceError(409, "CANNOT_CANCEL", "This transaction can't be cancelled now (%s)." % tx["state"],
                                   {"current_state": tx["state"]})
            app = self._app_for_tx(tx_id)
            self._move_tx(tx, "CANCELLED", "CUSTOMER", customer_id, {"closure_reason": "CANCELLED_BY_CUSTOMER"},
                          {"credit_state_at_cancel": app["credit_state"] if app else None}, "TRANSACTION_CANCELLED")
            self.notify(customer_id, "STATUS", "Purchase cancelled", "Nothing was charged.", "#/tx/" + tx_id, "cancel:" + tx_id)
        return self._customer_view(tx_id)

    # ------------------------------------------------------------ views
    def history(self, customer_id: str) -> List[Dict[str, Any]]:
        self.sweep_expired()
        self._customer(customer_id)
        return [self._customer_view(r["transaction_id"]) for r in
                self.db.all("SELECT transaction_id FROM transactions WHERE customer_id = ? ORDER BY created_at DESC, rowid DESC",
                            (customer_id,))]

    def audit_log(self, transaction_id: Optional[str] = None, limit: int = 300) -> List[Dict[str, Any]]:
        if transaction_id:
            rows = self.db.all("SELECT * FROM audit_events WHERE transaction_id = ? ORDER BY event_id", (transaction_id,))
        else:
            rows = self.db.all("SELECT * FROM audit_events ORDER BY event_id DESC LIMIT ?", (limit,))
        for r in rows:
            r["rule_ids"] = _j(r.pop("rule_ids_json"), [])
            r["details"] = _j(r.pop("details_json"), {})
        return rows

    def get_view(self, tx_id: str, customer_id: str) -> Dict[str, Any]:
        self.sweep_expired()
        self._tx(tx_id, customer_id)
        return self._customer_view(tx_id)

    def _customer_view(self, tx_id: str, already_assessed: bool = False) -> Dict[str, Any]:
        tx = self._tx(tx_id)
        app = self._app_for_tx(tx_id)
        merchant = self.db.one("SELECT merchant_id, name, category, city, icon, pay_later_eligible FROM merchants "
                               "WHERE merchant_id = ?", (tx["merchant_id"],))
        merchant["pay_later_eligible"] = bool(merchant["pay_later_eligible"])
        payments = self.db.all("SELECT payment_id, attempt_no, method, status, failure_code, failure_message, created_at, "
                               "completed_at FROM payments WHERE transaction_id = ? ORDER BY attempt_no", (tx_id,))
        credit = app["credit_state"] if app else None
        state = tx["state"]
        outcome = {
            "QR_CREATED": "TERMS_ACCEPTANCE" if not tx["tc_accepted_at"] else "CHECKOUT",
            "PAYMENT_METHOD_SELECTED": "CHECKOUT", "CONSENT_PENDING": "CHECKOUT",
            "CONSENT_COMPLETED": "READY_TO_ASSESS", "ASSESSMENT_IN_PROGRESS": "ASSESSING",
            "TERMS_PENDING_CONFIRMATION": "APPROVED_TERMS_PENDING", "DISBURSEMENT_PROCESSING": "PROCESSING",
            "PAYMENT_COMPLETED": "PAYMENT_COMPLETED", "PAYMENT_FAILED": "PAYMENT_FAILED", "EXPIRED": "EXPIRED",
        }.get(state)
        if state == "PENDING_REVIEW":
            outcome = "MORE_INFO_REQUIRED" if credit == "MORE_INFORMATION_REQUIRED" else "UNDER_REVIEW"
        if state == "CANCELLED":
            outcome = {"POLICY_STOP": "POLICY_STOP", "REJECTED": "REJECTED"}.get(credit, "CANCELLED")
        application = None
        if app:
            rd = _j(app["readiness_json"], {}) or {}
            rt = _j(app["routing_json"], {}) or {}
            msgs: List[str] = []
            for group in ("validation_errors", "verification_warnings", "evidence_gaps", "review_reasons"):
                for f in rd.get(group, []):
                    if f["customer_message"] not in msgs:
                        msgs.append(f["customer_message"])
            info_req = _j(app["info_request_json"]) if credit == "MORE_INFORMATION_REQUIRED" else None
            application = {
                "application_id": app["application_id"], "credit_state": credit,
                "readiness_status": app["readiness_status"],
                "readiness_explanation": rd.get("customer_explanation"),
                "decision_message": rt.get("customer_message"),
                "reasons_for_customer": msgs,
                "checks": [{"name": c["name"], "result": c["result"]} for c in rd.get("checks_performed", [])],
                "decided_by": "Automated demo engine" if (app["decided_by"] or "").endswith("ENGINE")
                else ("Credit officer" if (app["decided_by"] or "").startswith("OFFICER") else None),
                "info_request": {"items": info_req.get("items", []), "note": info_req.get("note"),
                                 "requested_by": "Credit officer" if str(info_req.get("requested_by", "")).startswith("OFFICER")
                                 else "Automated checks", "route": info_req.get("route"),
                                 "options": self._options(app)} if info_req else None,
                "documents": [{k: d[k] for k in ("doc_id", "doc_type", "file_name", "size_bytes", "status", "uploaded_at",
                                                 "submitted_at")} for d in self._documents("APPLICATION", app["application_id"])],
                "terms": _j(app["terms_json"]), "terms_confirmed_at": app["terms_confirmed_at"],
                "tenure_months": app["tenure_months"], "version": app["version"],
            }
        loan = self.db.one("SELECT loan_ref, principal_vnd, status FROM loans WHERE transaction_id = ?", (tx_id,))
        cust = self._customer(tx["customer_id"])
        seconds_left = None
        if tx["expires_at"] and state in S.TX_EXPIRABLE:
            seconds_left = max(0, int((datetime.fromisoformat(tx["expires_at"]) - self.clock()).total_seconds()))
        # Three separate, independently-derived milestones.
        progress = {
            "data_ready_for_assessment": None if not app or not app["readiness_status"]
            else app["readiness_status"] == "DATA_READY",
            "credit_approved": None if not app else (True if credit == "APPROVED" else
                                                      (False if credit in ("REJECTED", "POLICY_STOP") else None)),
            "payment_completed": state == "PAYMENT_COMPLETED",
        }
        return {
            "transaction": {k: tx[k] for k in ("transaction_id", "qr_ref", "amount_vnd", "description", "method",
                                                "tenure_months", "state", "closure_reason", "payment_ref", "created_at",
                                                "updated_at", "tc_accepted_at", "tc_version", "expires_at")},
            "merchant": merchant, "application": application, "payments": payments,
            "loan": loan, "outcome": outcome, "progress": progress, "seconds_left": seconds_left,
            "account": {"account_masked": cust["account_masked"], "balance_vnd": cust["balance_vnd"]},
            "credit": self.credit_summary(tx["customer_id"]),
            "already_assessed": already_assessed,
            "simulated": True,
        }
