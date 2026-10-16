"""Credit routing: turns a readiness result into a credit-decision route.

Routes: STP (automatic approval), customer action (missing details, supporting
documents or evidence options), human review, policy stop.
The engine never produces an automated credit rejection - only a credit
officer can reject. Hard stops are limited to explicitly configured rules.
Pure function; no I/O.
"""
from typing import Any, Dict, List

from . import readiness as R

ROUTE_STP = "STP"
ROUTE_REVIEW = "HUMAN_REVIEW"
ROUTE_MORE_INFO = "MORE_INFORMATION"
ROUTE_DOCUMENTS = "SUPPORTING_DOCUMENTS"
ROUTE_EVIDENCE = "EVIDENCE_OPTIONS"
ROUTE_POLICY_STOP = "POLICY_STOP"


def decide(readiness: Dict[str, Any], context: Dict[str, Any], policy: Dict[str, Any]) -> Dict[str, Any]:
    """context: {merchant: {pay_later_eligible, category}, requested_loan_vnd,
    loan_tenure_months, available_credit_vnd, third_party_available}

    Returns ``route``, the credit-state path (``state_path``), ``rule_ids``,
    officer-facing ``reasons``, a ``customer_message``, any ``requested_items``
    and the evaluation of each approval condition.
    """
    reasons: List[Dict[str, Any]] = []
    rule_ids: List[str] = []

    def stop(rule_id: str, msg: str, cmsg: str) -> Dict[str, Any]:
        return {
            "route": ROUTE_POLICY_STOP, "state_path": ["POLICY_STOP"], "final_credit_state": "POLICY_STOP",
            "rule_ids": [rule_id], "reasons": [{"rule_id": rule_id, "message": msg}], "approval_conditions": [],
            "customer_message": cmsg, "decided_by": "DECISION_ENGINE",
        }

    def customer_action(route: str, rids: List[str], why: List[Dict[str, Any]], items: List[Dict[str, Any]],
                        cmsg: str) -> Dict[str, Any]:
        return {
            "route": route, "state_path": ["MORE_INFORMATION_REQUIRED"], "final_credit_state": "MORE_INFORMATION_REQUIRED",
            "rule_ids": sorted(set(rids)), "reasons": why, "approval_conditions": [], "customer_message": cmsg,
            "requested_items": items, "decided_by": "DECISION_ENGINE",
        }

    # 1. Explicit hard stops only.
    if readiness.get("missing_consent"):
        return stop("HS-01", "Required data basis missing: " + ", ".join(readiness["missing_consent"]),
                    "We can't use the information needed for Pay Later, so we can't continue. No money has been taken.")
    merchant = context.get("merchant") or {}
    if not merchant.get("pay_later_eligible", False):
        return stop("HS-02", "Merchant category '%s' is not eligible for Pay Later (demo restriction)." % merchant.get("category"),
                    "Pay Later can't be used at this merchant. You can still pay from your account.")

    status = readiness["status"]
    docs_cfg = policy["documents"]

    # 2. Missing mandatory information -> ask the customer, not a decline.
    if status == R.INCOMPLETE:
        return customer_action(ROUTE_MORE_INFO, ["R-INC-01"],
                               [{"rule_id": "R-INC-01", "message": "Missing: " + ", ".join(readiness["missing_fields"])}],
                               [{"field": f, "type": "field"} for f in readiness["missing_fields"]],
                               "We need a few more details before we can assess your request.")

    # 3. Invalid, unverified, conflicting or pending-document data -> credit officer.
    if status in (R.INVALID_DATA, R.VERIFY_DATA):
        for group in ("validation_errors", "verification_warnings", "review_reasons"):
            for f in readiness[group]:
                reasons.append({"rule_id": f["rule_id"], "message": f["message"]})
                rule_ids.append(f["rule_id"])
        return _review(reasons, rule_ids, ["MANUAL_REVIEW"])

    # 4. Not enough evidence -> the customer chooses how to add it.
    if status == R.INSUFFICIENT_EVIDENCE:
        why = [{"rule_id": f["rule_id"], "message": f["message"]} for f in readiness["evidence_gaps"] + readiness["review_reasons"]]
        options = (["third_party_consent"] if context.get("third_party_available") else []) + ["documents", "manual_review"]
        items = [{"type": "evidence_options", "options": options}] + \
                [{"type": "document", "document": d, "label": docs_cfg["types"][d]} for d in docs_cfg["default_request_for_evidence"]]
        return customer_action(ROUTE_EVIDENCE, [w["rule_id"] for w in why], why, items,
                               "We don't have enough verified information to assess this purchase yet. You can choose how "
                               "to add it. Nothing has been charged.")

    # 5. DATA_READY but a profile risk flag -> supporting documents first, then an officer.
    if readiness["review_reasons"]:
        why = [{"rule_id": f["rule_id"], "message": f["message"]} for f in readiness["review_reasons"]]
        items = [{"type": "document", "document": d, "label": docs_cfg["types"][d]} for d in docs_cfg["default_request_for_risk_flags"]] + \
                [{"type": "evidence_options", "options": ["documents", "manual_review"]}]
        return customer_action(ROUTE_DOCUMENTS, [w["rule_id"] for w in why], why, items,
                               "We need to verify your financial situation before deciding. Please upload supporting "
                               "documents; a credit officer will review them. Uploading documents does not guarantee approval.")

    # 6. DATA_READY and nothing to review -> STP candidate. Approval still
    #    requires every configured demo approval condition.
    available = context.get("available_credit_vnd")
    amount = context.get("requested_loan_vnd")
    offered = policy["repayment_terms"]["offered_tenures"]
    conditions = [
        {"id": "AC-01", "label": "Data readiness is DATA_READY", "passed": status == R.DATA_READY},
        {"id": "AC-02", "label": "No profile risk flag or review rule triggered", "passed": not readiness["review_reasons"]},
        {"id": "AC-03", "label": "Amount within available credit (fictional limit minus outstanding balance)",
         "passed": available is not None and amount is not None and amount <= available,
         "detail": "available credit not configured (unresolved)" if available is None else "%s <= %s" % (amount, available)},
        {"id": "AC-04", "label": "Tenure offered for demo product", "passed": context.get("loan_tenure_months") in offered},
    ]
    failed = [c for c in conditions if not c["passed"]]
    if failed:
        fr = [{"rule_id": c["id"], "message": "STP approval condition not met: " + c["label"]
               + (" (" + c["detail"] + ")" if c.get("detail") else "")} for c in failed]
        out = _review(fr, [c["id"] for c in failed], ["STP_ELIGIBLE", "MANUAL_REVIEW"])
        out["approval_conditions"] = conditions
        out["customer_message"] = ("Your information checks out, but this request needs a credit officer's review "
                                   "before it can be approved. No money has been taken.")
        return out

    return {
        "route": ROUTE_STP, "state_path": ["STP_ELIGIBLE", "APPROVED"], "final_credit_state": "APPROVED",
        "rule_ids": [c["id"] for c in conditions],
        "reasons": [{"rule_id": "STP", "message": "All data checks and configured demo approval conditions passed."}],
        "approval_conditions": conditions,
        "customer_message": "Approved. Please review and confirm the repayment terms to complete your payment.",
        "decided_by": "STP_ENGINE",
    }


def _review(reasons: List[Dict[str, Any]], rule_ids: List[str], path: List[str]) -> Dict[str, Any]:
    return {
        "route": ROUTE_REVIEW, "state_path": path, "final_credit_state": "MANUAL_REVIEW",
        "rule_ids": sorted(set(rule_ids)), "reasons": reasons, "approval_conditions": [],
        "customer_message": "Your request needs an additional review by a credit officer. "
                            "Your purchase is on hold and no money has been taken.",
        "decided_by": "DECISION_ENGINE",
    }
