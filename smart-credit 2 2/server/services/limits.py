"""Credit-limit increase requests: a separate review case. The limit changes only
after a credit-officer approval; documents never guarantee approval."""
import json
from typing import Any, Dict, List, Optional

from ..domain import states as S
from ..store.db import now_iso
from .base import ServiceError, _j


class LimitMixin:
    def _limit_req(self, request_id: str, customer_id: Optional[str] = None) -> Dict[str, Any]:
        r = self.db.one("SELECT * FROM limit_requests WHERE request_id = ?", (request_id,))
        if not r:
            raise ServiceError(404, "LIMIT_REQUEST_NOT_FOUND", "Limit request %s not found." % request_id)
        if customer_id is not None and r["customer_id"] != customer_id:
            raise ServiceError(403, "FORBIDDEN", "This request belongs to another demo customer.")
        return r

    def _limit_view(self, r: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(r)
        out["info_request"] = _j(out.pop("info_request_json"))
        out["documents"] = [{k: d[k] for k in ("doc_id", "doc_type", "file_name", "size_bytes", "status", "uploaded_at",
                                                "submitted_at")} for d in self._documents("LIMIT_REQUEST", r["request_id"])]
        return out

    def _move_limit(self, r: Dict[str, Any], target: str, actor: str, actor_id: str, extra: Optional[Dict[str, Any]] = None,
                    expected_version: Optional[int] = None, note: Optional[str] = None) -> None:
        try:
            S.check_limit(r["status"], target)
        except S.InvalidTransition as e:
            raise ServiceError(409, "INVALID_LIMIT_STATE", str(e), {"current_state": r["status"]})
        fields = {"status": target}
        fields.update(extra or {})
        if self.db.update("limit_requests", "request_id", r["request_id"], fields,
                          r["version"] if expected_version is None else expected_version) != 1:
            raise ServiceError(409, "STALE_DECISION", "This request changed since you opened it. Reload before acting.")
        self.db.audit(actor, actor_id, "LIMIT_REQUEST_STATE_CHANGED", r["linked_transaction_id"], None, r["status"], target,
                      details={"request_id": r["request_id"], "note": note})

    # ------------------------------------------------------------ customer
    def list_limit_requests(self, customer_id: str) -> List[Dict[str, Any]]:
        return [self._limit_view(r) for r in self.db.all("SELECT * FROM limit_requests WHERE customer_id = ? "
                                                         "ORDER BY created_at DESC, rowid DESC", (customer_id,))]

    def get_limit_request(self, request_id: str, customer_id: str) -> Dict[str, Any]:
        return self._limit_view(self._limit_req(request_id, customer_id))

    def create_limit_request(self, customer_id: str, requested_limit: Any, reason: str,
                             linked_tx: Optional[str] = None) -> Dict[str, Any]:
        credit = self.credit_summary(customer_id)
        if credit["open_limit_request"]:
            raise ServiceError(409, "LIMIT_REQUEST_OPEN", "You already have an open limit request.",
                               {"request_id": credit["open_limit_request"]["request_id"]})
        mx = self.policy["credit_limit"]["max_limit_vnd"]
        if not isinstance(requested_limit, int) or isinstance(requested_limit, bool) or \
                not (credit["limit_vnd"] < requested_limit <= mx):
            raise ServiceError(422, "INVALID_LIMIT", "Requested limit must be above your current limit (VND {:,}) and at most "
                               "VND {:,} (demo maximum).".format(credit["limit_vnd"], mx))
        if linked_tx:
            self._tx(linked_tx, customer_id)
        with self._unit():
            rid = self._new_id("CLR", "limit")
            ts = now_iso()
            self.db.run("INSERT INTO limit_requests(request_id, customer_id, current_limit_vnd, outstanding_vnd, "
                        "requested_limit_vnd, reason, linked_transaction_id, status, created_at, updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (rid, customer_id, credit["limit_vnd"], credit["outstanding_vnd"], requested_limit,
                         (reason or "")[:500], linked_tx, "DRAFT", ts, ts))
            self.db.audit("CUSTOMER", customer_id, "LIMIT_REQUEST_CREATED", linked_tx,
                          details={"request_id": rid, "current_limit_vnd": credit["limit_vnd"],
                                   "requested_limit_vnd": requested_limit})
        return self.get_limit_request(rid, customer_id)

    def upload_limit_document(self, request_id: str, customer_id: str, doc: Dict[str, Any]) -> Dict[str, Any]:
        with self._unit():
            r = self._limit_req(request_id, customer_id)
            if r["status"] not in ("DRAFT", "MORE_INFORMATION_REQUIRED"):
                raise ServiceError(409, "INVALID_LIMIT_STATE", "Documents can't be added in status %s." % r["status"])
            self._add_document("LIMIT_REQUEST", request_id, customer_id, doc)
        return self.get_limit_request(request_id, customer_id)

    def submit_limit_request(self, request_id: str, customer_id: str) -> Dict[str, Any]:
        with self._unit():
            r = self._limit_req(request_id, customer_id)
            if r["status"] not in ("DRAFT", "MORE_INFORMATION_REQUIRED"):
                raise ServiceError(409, "INVALID_LIMIT_STATE", "This request has already been submitted.")
            new_docs = [d for d in self._documents("LIMIT_REQUEST", request_id) if d["status"] == "UPLOADED"]
            if r["status"] == "MORE_INFORMATION_REQUIRED":
                wanted = [i["document"] for i in (_j(r["info_request_json"], {}) or {}).get("items", [])]
                missing = [self.policy["documents"]["types"][t] for t in wanted if t not in {d["doc_type"] for d in new_docs}]
                if missing:
                    raise ServiceError(422, "INFORMATION_INCOMPLETE", "Please upload: %s" % ", ".join(missing))
            elif not new_docs and not self._documents("LIMIT_REQUEST", request_id):
                raise ServiceError(422, "DOCUMENTS_REQUIRED", "Upload at least one supporting document (e.g. a payslip).")
            ts = now_iso()
            for d in new_docs:
                self.db.run("UPDATE documents SET status = 'SUBMITTED', submitted_at = ? WHERE doc_id = ?", (ts, d["doc_id"]))
            self.db.audit("CUSTOMER", customer_id, "DOCUMENTS_SUBMITTED", r["linked_transaction_id"],
                          details={"request_id": request_id, "documents": [d["doc_id"] for d in new_docs]})
            self._move_limit(r, "PENDING_REVIEW", "CUSTOMER", customer_id)
            self.notify(customer_id, "STATUS", "Limit increase request submitted",
                        "A credit officer will review it. Submitting documents does not guarantee approval.",
                        "#/limit", "limit-submit:%s:%s" % (request_id, r["version"]))
        return self.get_limit_request(request_id, customer_id)

    def cancel_limit_request(self, request_id: str, customer_id: str) -> Dict[str, Any]:
        with self._unit():
            r = self._limit_req(request_id, customer_id)
            self._move_limit(r, "CANCELLED", "CUSTOMER", customer_id)
        return self.get_limit_request(request_id, customer_id)

    # ------------------------------------------------------------ officer
    def officer_limit_requests(self) -> Dict[str, List[Dict[str, Any]]]:
        rows = self.db.all("SELECT r.*, c.display_name FROM limit_requests r JOIN customers c USING(customer_id) "
                           "WHERE r.status != 'DRAFT' ORDER BY r.updated_at DESC, r.rowid DESC")
        g: Dict[str, List[Dict[str, Any]]] = {"pending_review": [], "awaiting_customer": [], "decided": []}
        for r in rows:
            item = {k: r[k] for k in ("request_id", "customer_id", "display_name", "current_limit_vnd", "requested_limit_vnd",
                                      "approved_limit_vnd", "status", "updated_at", "version")}
            g["pending_review" if r["status"] == "PENDING_REVIEW" else
              "awaiting_customer" if r["status"] == "MORE_INFORMATION_REQUIRED" else "decided"].append(item)
        return g

    def officer_limit_request(self, request_id: str) -> Dict[str, Any]:
        r = self._limit_req(request_id)
        out = self._limit_view(r)
        out["can_decide"] = r["status"] == "PENDING_REVIEW"
        out["profile"] = self._profile_panel(r["customer_id"])
        out["audit"] = [e for e in self.audit_log(limit=500) if (e["details"] or {}).get("request_id") == request_id][::-1]
        return out

    def officer_limit_decide(self, request_id: str, officer_id: str, action: str, note: Optional[str],
                             expected_version: Any, approved_limit: Any = None,
                             requested_documents: Optional[List[str]] = None) -> Dict[str, Any]:
        if action not in ("approve", "reject", "request_info"):
            raise ServiceError(422, "INVALID_ACTION", "Action must be approve, reject or request_info.")
        if not isinstance(expected_version, int):
            raise ServiceError(422, "VERSION_REQUIRED", "expected_version is required to prevent stale decisions.")
        if action in ("reject", "request_info") and not (note or "").strip():
            raise ServiceError(422, "NOTE_REQUIRED", "A note is required for this action.")
        with self._unit():
            r = self._limit_req(request_id)
            if r["version"] != expected_version:
                raise ServiceError(409, "STALE_DECISION", "This request changed since you opened it. Reload before deciding.")
            if r["status"] != "PENDING_REVIEW":
                raise ServiceError(409, "INVALID_LIMIT_STATE", "Only requests pending review can be decided.")
            cust = self._customer(r["customer_id"])
            if action == "approve":
                if not isinstance(approved_limit, int) or isinstance(approved_limit, bool) or \
                        not (cust["indicative_limit_vnd"] < approved_limit <= r["requested_limit_vnd"]):
                    raise ServiceError(422, "INVALID_LIMIT", "Approved limit must be above the current limit and at most the "
                                       "requested limit.")
                self._move_limit(r, "APPROVED", "OFFICER", officer_id, {"approved_limit_vnd": approved_limit,
                                 "decided_by": "OFFICER:" + officer_id, "decision_note": note}, expected_version, note)
                self.db.run("UPDATE customers SET indicative_limit_vnd = ? WHERE customer_id = ?", (approved_limit, r["customer_id"]))
                for d in self._documents("LIMIT_REQUEST", request_id):
                    if d["status"] == "SUBMITTED":
                        self.db.run("UPDATE documents SET status = 'ACCEPTED' WHERE doc_id = ?", (d["doc_id"],))
                self.db.audit("OFFICER", officer_id, "CREDIT_LIMIT_CHANGED", r["linked_transaction_id"],
                              details={"request_id": request_id, "from_vnd": cust["indicative_limit_vnd"],
                                       "to_vnd": approved_limit, "note": note})
                self.notify(r["customer_id"], "STATUS", "Credit limit increased",
                            "Your fictional Pay Later limit is now VND {:,}. Available credit is recalculated against your "
                            "outstanding balance.".format(approved_limit), "#/limit", "limit-approved:" + request_id)
            elif action == "reject":
                self._move_limit(r, "REJECTED", "OFFICER", officer_id, {"decided_by": "OFFICER:" + officer_id,
                                 "decision_note": note}, expected_version, note)
                self.notify(r["customer_id"], "STATUS", "Limit increase not approved",
                            "Your current limit stays VND {:,}. You can still use Pay Now.".format(cust["indicative_limit_vnd"]),
                            "#/limit", "limit-rejected:" + request_id)
            else:
                docs = requested_documents or ["payslip"]
                types = self.policy["documents"]["types"]
                if any(d not in types for d in docs):
                    raise ServiceError(422, "INVALID_DOCUMENT", "Unknown document type.")
                req = {"requested_by": "OFFICER:" + officer_id, "ts": now_iso(), "note": note,
                       "items": [{"type": "document", "document": d, "label": types[d]} for d in docs]}
                self._move_limit(r, "MORE_INFORMATION_REQUIRED", "OFFICER", officer_id, {"info_request_json": json.dumps(req)},
                                 expected_version, note)
                self.notify(r["customer_id"], "ACTION", "More documents needed for your limit request", note or "",
                            "#/limit", "limit-info:%s:%s" % (request_id, r["version"]))
        return self.officer_limit_request(request_id)
