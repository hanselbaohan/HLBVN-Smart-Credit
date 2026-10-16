"""Data-readiness assessment.

Answers ONE question: can the available information be trusted and used for
assessment?  It never decides credit.  DATA_READY means "ready to be
assessed", not "approved".

Pure functions only: no database, no clock (``as_of`` is passed in), no I/O.
"""
from datetime import date, datetime
from typing import Any, Dict, List, Optional

INCOMPLETE = "INCOMPLETE"
INVALID_DATA = "INVALID_DATA"
VERIFY_DATA = "VERIFY_DATA"
INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
DATA_READY = "DATA_READY"

STATUS_ORDER = [INCOMPLETE, INVALID_DATA, VERIFY_DATA, INSUFFICIENT_EVIDENCE, DATA_READY]

CUSTOMER_STATUS_TEXT = {
    INCOMPLETE: "Some required details are missing, so we can't assess this request yet.",
    INVALID_DATA: "Some details don't look right, so a credit officer needs to check them.",
    VERIFY_DATA: "Some of your financial information needs to be verified before we can use it.",
    INSUFFICIENT_EVIDENCE: "We don't have enough verified information yet to assess this request.",
    DATA_READY: "Your information is complete and verified, so it's ready for assessment. This is not an approval.",
}


def _is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _parse_date(value: Any) -> Optional[date]:
    if _is_missing(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _finding(rule_id: str, field: Optional[str], message: str, customer_message: str) -> Dict[str, Any]:
    return {"rule_id": rule_id, "field": field, "message": message, "customer_message": customer_message}


def evaluate(application: Dict[str, Any],
             applicant: Dict[str, Any],
             sources: Dict[str, Dict[str, Any]],
             consent: Optional[Dict[str, Any]],
             policy: Dict[str, Any],
             as_of: date) -> Dict[str, Any]:
    """Return a structured readiness result. See docs/API.md for the schema."""
    checks: List[Dict[str, Any]] = []
    validation_errors: List[Dict[str, Any]] = []
    verification_warnings: List[Dict[str, Any]] = []
    evidence_gaps: List[Dict[str, Any]] = []
    review_reasons: List[Dict[str, Any]] = []
    missing_fields: List[str] = []
    unresolved: List[Dict[str, Any]] = []
    unused_fields: Dict[str, str] = {}

    def check(check_id: str, name: str, result: str, detail: str) -> None:
        checks.append({"check_id": check_id, "name": name, "result": result, "detail": detail})

    applicant = dict(applicant or {})
    sources = sources or {}
    granted = set((consent or {}).get("categories") or []) if (consent or {}).get("granted") else set()

    # C00 - leakage guard: downstream outcome fields are never inputs.
    excluded = [f for f in policy["excluded_downstream_fields"]["fields"] if f in applicant]
    for f in excluded:
        applicant.pop(f, None)
    check("C00", "Outcome fields excluded from inputs", "PASS",
          "Removed: " + ", ".join(excluded) if excluded else "No outcome fields supplied.")

    # C01 - consent / source authorisation. Data from a source whose consent
    # category was not granted is treated as unavailable.
    source_cfg = policy["data_sources"]
    for src_id, cfg in source_cfg.items():
        if cfg["consent_category"] not in granted:
            for f in cfg["fields"]:
                if f in applicant and not _is_missing(applicant[f]):
                    unused_fields[f] = "no consent for " + cfg["consent_category"]
                    applicant[f] = None
    required_consent = policy["consent"]["required_categories"]
    missing_consent = [c for c in required_consent if c not in granted]
    check("C01", "Customer consent and source authorisation",
          "FAIL" if missing_consent else "PASS",
          ("Missing consent: " + ", ".join(missing_consent)) if missing_consent
          else "Required categories granted: " + ", ".join(required_consent))

    # C02 - mandatory information.
    mand = policy["mandatory_fields"]
    for f in mand["application"]:
        if _is_missing(application.get(f)):
            missing_fields.append(f)
    for f in mand["applicant"]:
        if _is_missing(applicant.get(f)):
            missing_fields.append(f)
    for f in missing_fields:
        validation_errors.append(_finding("R-INC-01", f, "Mandatory field '%s' is missing." % f,
                                          "A required detail is missing: %s." % f.replace("_", " ")))
    check("C02", "Mandatory application and purchase information",
          "FAIL" if missing_fields else "PASS",
          ("Missing: " + ", ".join(missing_fields)) if missing_fields else "All mandatory fields present.")

    # C03 - formats and documented valid ranges.
    ranges = policy["case_ranges"]
    invalid: List[Dict[str, Any]] = []

    def invalid_value(field: str, msg: str, cmsg: str) -> None:
        invalid.append(_finding("R-INV-01", field, msg, cmsg))

    amt = application.get("requested_loan_vnd")
    if not _is_missing(amt):
        r = ranges["requested_loan_vnd"]
        if not _is_number(amt) or int(amt) != amt:
            invalid_value("requested_loan_vnd", "Requested amount must be a whole VND number.",
                          "The purchase amount is not in a valid format.")
        elif not (r["min"] <= amt <= r["max"]):
            invalid_value("requested_loan_vnd",
                          "Requested amount %s is outside the documented case range %s-%s VND." % (amt, r["min"], r["max"]),
                          "Pay Later in this demo supports purchases from VND 1,600,000 to VND 90,100,000.")
    ten = application.get("loan_tenure_months")
    if not _is_missing(ten) and ten not in ranges["loan_tenure_months"]["allowed"]:
        invalid_value("loan_tenure_months",
                      "Tenure %s is not a supported demo tenure %s." % (ten, ranges["loan_tenure_months"]["allowed"]),
                      "The selected repayment period isn't available.")
    score = applicant.get("unified_credit_score")
    if not _is_missing(score):
        r = ranges["unified_credit_score"]
        if not _is_number(score) or not (r["min"] <= score <= r["max"]):
            invalid_value("unified_credit_score", "Credit score %s outside the %s-%s scale." % (score, r["min"], r["max"]),
                          "A credit information value could not be validated.")
    bill = applicant.get("on_time_bill_ratio")
    if not _is_missing(bill):
        r = ranges["on_time_bill_ratio"]
        if not _is_number(bill) or not (r["min"] <= bill <= r["max"]):
            invalid_value("on_time_bill_ratio", "Bill payment ratio %s must be between 0 and 1." % bill,
                          "A bill-payment value could not be validated.")
    for f in ranges["binary_fields"]["fields"]:
        v = applicant.get(f)
        if not _is_missing(v) and (not _is_number(v) or v not in ranges["binary_fields"]["allowed"]):
            invalid_value(f, "Binary field '%s' must be 0 or 1 (got %r)." % (f, v),
                          "A yes/no value in your profile could not be validated.")
    for f in ranges["non_negative_fields"]["fields"]:
        v = applicant.get(f)
        if not _is_missing(v) and (not _is_number(v) or v < 0):
            invalid_value(f, "Field '%s' must be a non-negative number (got %r)." % (f, v),
                          "A financial value could not be validated.")
    prof = applicant.get("customer_profile")
    if not _is_missing(prof) and prof not in ranges["customer_profiles"]["allowed"]:
        invalid_value("customer_profile", "Unknown customer profile %r." % prof,
                      "Your customer profile could not be validated.")
    for key in ("inflow_cv_upper_bound", "dti_ratio_upper_bound"):
        if ranges[key]["source"] == "UNRESOLVED":
            unresolved.append({"config": "case_ranges." + key, "note": ranges[key]["note"]})
    validation_errors.extend(invalid)
    check("C03", "Valid formats and documented ranges", "FAIL" if invalid else "PASS",
          "; ".join(i["message"] for i in invalid) if invalid else "All supplied values are within documented ranges.")

    # C04 - has_observable_tx consistency. Values are kept but not trusted.
    dep_fields = policy["transaction_dependent_fields"]["fields"]
    # Fields supplied by a consented, verified THIRD-PARTY source are not internal
    # transaction records, so they don't contradict has_observable_tx = 0.
    third_party_fields = set()
    for src_id in policy["consent"].get("third_party_categories", []):
        if src_id in granted and (sources.get(src_id) or {}).get("verified"):
            third_party_fields |= set(source_cfg[src_id]["fields"])
    if applicant.get("has_observable_tx") == 0:
        populated = [f for f in dep_fields if _is_number(applicant.get(f)) and applicant.get(f) != 0
                     and f not in third_party_fields]
        if populated:
            verification_warnings.append(_finding(
                "R-VER-01", "has_observable_tx",
                "has_observable_tx = 0 but transaction fields are populated (%s). Values retained, not trusted." % ", ".join(populated),
                "Your transaction information doesn't match our records and needs to be checked."))
            for f in populated:
                unused_fields.setdefault(f, "conflicts with has_observable_tx = 0")
            check("C04", "Transaction-observability consistency", "WARN",
                  "Populated while has_observable_tx = 0: " + ", ".join(populated))
        else:
            check("C04", "Transaction-observability consistency", "PASS", "No observable transactions and no transaction values.")
    else:
        check("C04", "Transaction-observability consistency", "PASS",
              "Transaction data marked observable." if applicant.get("has_observable_tx") == 1 else "Not evaluated (flag missing).")

    # C05/C06 - provenance, verification and freshness of every source used.
    max_age = policy["freshness"]["max_age_days"]["value"]
    need_period = policy["freshness"]["require_observation_period"]["value"]
    prov_issues: List[str] = []
    fresh_issues: List[str] = []
    for src_id, cfg in source_cfg.items():
        used = [f for f in cfg["fields"] if not _is_missing(applicant.get(f))]
        src = sources.get(src_id)
        if not used and not (src and src_id == "supporting_documents"):
            continue
        if src is None:
            prov_issues.append(src_id)
            verification_warnings.append(_finding(
                "R-VER-02", src_id, "Fields %s have no known source (unknown provenance)." % ", ".join(used),
                "We couldn't confirm where some of your financial information came from."))
            continue
        if cfg["consent_category"] not in granted:
            continue
        if not src.get("verified"):
            prov_issues.append(src_id)
            if src_id == "supporting_documents":
                verification_warnings.append(_finding(
                    "R-DOC-01", src_id, "Supporting documents submitted (%s); pending credit-officer verification."
                    % ", ".join(src.get("documents") or []),
                    "We've received your documents. A credit officer will verify them before any decision."))
            else:
                verification_warnings.append(_finding(
                    "R-VER-02", src_id, "Source '%s' is not verified." % src_id,
                    "Some of your financial information hasn't been verified yet."))
        observed = _parse_date(src.get("observed_at"))
        if observed is None:
            fresh_issues.append(src_id)
            verification_warnings.append(_finding(
                "R-VER-03", src_id, "Source '%s' has no observation date." % src_id,
                "We couldn't confirm how recent some of your information is."))
        elif max_age is not None and (as_of - observed).days > max_age:
            fresh_issues.append(src_id)
            verification_warnings.append(_finding(
                "R-VER-03", src_id, "Source '%s' is %d days old (demo limit %d)." % (src_id, (as_of - observed).days, max_age),
                "Some of your financial information is out of date and needs refreshing."))
        if need_period and src_id == "hlb_transactions" and applicant.get("has_observable_tx") == 1 \
                and not src.get("period_months"):
            fresh_issues.append(src_id)
            verification_warnings.append(_finding(
                "R-VER-03", src_id, "Transaction source has no observation period.",
                "We couldn't confirm the period covered by your transaction history."))
    check("C05", "Data sources known and verified", "WARN" if prov_issues else "PASS",
          ("Unverified/unknown: " + ", ".join(prov_issues)) if prov_issues else "All sources used are verified.")
    check("C06", "Data freshness and observation period", "WARN" if fresh_issues else "PASS",
          ("Issues: " + ", ".join(fresh_issues)) if fresh_issues
          else "All sources observed within %s days." % max_age)

    # C07 - internal consistency between related fields.
    rule = policy["consistency_rules"]["profile_requires_cic"]
    cic = applicant.get("has_formal_cic_history")
    if prof in rule and cic in (0, 1) and rule[prof] != cic:
        verification_warnings.append(_finding(
            "R-VER-04", "customer_profile",
            "Profile '%s' conflicts with has_formal_cic_history = %s." % (prof, cic),
            "Your profile and credit history records don't match and need to be checked."))
        check("C07", "Related fields are consistent", "WARN", "Profile vs CIC history conflict.")
    else:
        check("C07", "Related fields are consistent", "PASS", "No conflicts between related fields.")

    # C08 - profile risk flags / mandatory-review signals. They trigger
    # supporting-document verification and a credit officer - never a decline.
    ops = {"eq": lambda a, b: a == b, "lt": lambda a, b: a < b, "gt": lambda a, b: a > b}
    for rr in policy["review_rules"]:
        v = applicant.get(rr["field"])
        if _is_number(v) and ops[rr["op"]](v, rr["value"]):
            f = _finding(rr["rule_id"], rr["field"], rr["message"], rr["customer_message"])
            f["source"] = rr["source"]
            review_reasons.append(f)
    check("C08", "Profile risk flags & review signals", "WARN" if review_reasons else "PASS",
          "; ".join(r["rule_id"] for r in review_reasons) if review_reasons else "None triggered.")

    # C09 - CIC note: missing formal history is recorded, never a decline reason.
    if cic == 0:
        check("C09", "Formal CIC history", "INFO",
              "No formal CIC history. Not used as a rejection reason (R-CIC-01); other evidence paths are considered.")
    else:
        check("C09", "Formal CIC history", "PASS" if cic == 1 else "INFO",
              "Formal history present." if cic == 1 else "Not evaluated.")

    # C10 - evidence sufficiency (explicitly configured evidence paths).
    req = policy["evidence_requirement"]
    met_paths: List[str] = []
    path_notes: List[str] = []
    for path in req["any_of"]:
        src = sources.get(path["source"])
        cat = source_cfg[path["source"]]["consent_category"]
        ok = bool(src) and bool(src.get("verified")) and cat in granted and path["source"] not in fresh_issues
        for f, v in (path.get("requires_field") or {}).items():
            ok = ok and applicant.get(f) == v
        if path.get("requires_field_present"):
            ok = ok and not _is_missing(applicant.get(path["requires_field_present"]))
        if path["id"] == "EV-3P" and ok:
            ok = (src.get("period_months") or 0) >= path.get("min_period_months", 0)
        if path["id"] == "EV-TX" and ok:
            ok = (src.get("period_months") or 0) >= path.get("min_period_months", 0) \
                and "has_observable_tx" not in [w["field"] for w in verification_warnings]
        if ok:
            met_paths.append(path["id"])
        else:
            path_notes.append(path["id"] + " not available: " + path["label"])
    if not met_paths:
        evidence_gaps.append(_finding(
            "R-EVD-01", None,
            "No configured evidence path satisfied (" + "; ".join(path_notes) + ").",
            "We don't have enough verified information yet to assess this request."))
    check("C10", "Evidence sufficiency", "PASS" if met_paths else "FAIL",
          ("Satisfied by: " + ", ".join(met_paths)) if met_paths else "No evidence path satisfied.")

    # Status precedence.
    if missing_fields:
        status = INCOMPLETE
    elif invalid:
        status = INVALID_DATA
    elif verification_warnings:
        status = VERIFY_DATA
    elif evidence_gaps:
        status = INSUFFICIENT_EVIDENCE
    else:
        status = DATA_READY

    explanation = {
        INCOMPLETE: "Mandatory information is missing: %s." % ", ".join(missing_fields),
        INVALID_DATA: "One or more values are outside documented valid ranges or formats.",
        VERIFY_DATA: "Data is complete and valid but some of it is unverified, stale, of unknown origin or conflicting.",
        INSUFFICIENT_EVIDENCE: "Data is valid and verified but no configured evidence path is satisfied.",
        DATA_READY: "Data passed all quality checks and meets the configured evidence requirement. "
                    "This is readiness for assessment, not a credit approval.",
    }[status]

    return {
        "status": status,
        "validation_errors": validation_errors,
        "verification_warnings": verification_warnings,
        "evidence_gaps": evidence_gaps,
        "review_reasons": review_reasons,
        "checks_performed": checks,
        "explanation": explanation,
        "customer_explanation": CUSTOMER_STATUS_TEXT[status],
        "missing_fields": missing_fields,
        "missing_consent": missing_consent,
        "evidence_paths_met": met_paths,
        "fields_not_used": unused_fields,
        "excluded_outcome_fields": excluded,
        "unresolved_configuration": unresolved,
        "policy_version": policy["policy_version"],
    }
