"""End-to-end persona walkthroughs against a RUNNING server, over HTTP.

Usage:  python3 scripts/e2e_scenarios.py [http://127.0.0.1:8080]

Resets the demo database, drives every test scenario exactly as the UI does
(same endpoints, same order), asserts the outcome, and writes a verifiable
log to docs/test_logs/e2e_<timestamp>.md (+ .json). Each step records the HTTP
call, status code, resulting states and the audit-event ids it produced, so a
reviewer can re-run the script and compare.

These are MOCK-PERSONA tests (fictional customers derived from dataset rows).
They are not usability tests with real people.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080").rstrip("/")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OFFICER = {"X-Demo-Officer-Code": "DEMO-OFFICER-2026", "X-Demo-Officer": "officer.e2e"}
QR = {}
LOG = []
CURRENT = {"scenario": None}


def call(method, path, body=None, cust=None, headers=None, note=""):
    h = {"Content-Type": "application/json"}
    if cust:
        h["X-Demo-Customer"] = cust
    h.update(headers or {})
    data = json.dumps(body if body is not None else {}).encode() if method == "POST" else None
    before = last_event()
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req) as r:
            status, out = r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        status, out = e.code, json.loads(e.read())
    after = last_event()
    entry = {"scenario": CURRENT["scenario"], "actor": cust or ("officer" if "X-Demo-Officer-Code" in h else "demo"),
             "call": "%s %s" % (method, path), "status": status, "note": note,
             "audit_events": [before + 1, after] if after > before else None}
    if isinstance(out, dict):
        if "error" in out:
            entry["result"] = out["error"]["code"]
        elif "outcome" in out:
            entry["result"] = "%s | tx=%s | credit=%s | readiness=%s" % (
                out["outcome"], out["transaction"]["state"], (out.get("application") or {}).get("credit_state"),
                (out.get("application") or {}).get("readiness_status"))
        elif "status" in out and "request_id" in out:
            entry["result"] = "limit request %s" % out["status"]
        elif "loan_ref" in out:
            entry["result"] = "loan %s paid %s/%s outstanding %s" % (out["status"], out["paid_count"], out["instalment_count"],
                                                                     out["outstanding_vnd"])
    if path != "/api/officer/audit":
        LOG.append(entry)
    return status, out


def last_event():
    try:
        req = urllib.request.Request(BASE + "/api/officer/audit", headers=OFFICER)
        with urllib.request.urlopen(req) as r:
            ev = json.loads(r.read())
        return ev[0]["event_id"] if ev else 0
    except Exception:
        return 0


def expect(cond, msg):
    if not cond:
        raise AssertionError(msg)


def scan(cust, ref, accept=True):
    st, r = call("POST", "/api/qr/resolve", {"payload": QR[ref]}, cust, note="scan " + ref)
    expect(st in (200, 201), r)
    tx = r["view"]["transaction"]["transaction_id"]
    if accept:
        call("POST", "/api/transactions/%s/terms-and-conditions" % tx, {"accepted": True}, cust, note="tick T&C checkbox")
    return tx


def pay_later(cust, ref, tenure=6):
    tx = scan(cust, ref)
    st, v = call("POST", "/api/transactions/%s/payment-method" % tx, {"method": "PAY_LATER", "tenure_months": tenure}, cust,
                 note="choose Pay Later")
    expect(st == 200, v)
    _, v = call("POST", "/api/applications/%s/assess" % v["application"]["application_id"], None, cust, note="assess")
    return tx, v


def confirm_pay(cust, tx, v, tenure=6):
    call("POST", "/api/applications/%s/terms/confirm" % v["application"]["application_id"], {"tenure_months": tenure, "accepted": True},
         cust, note="confirm terms")
    return call("POST", "/api/transactions/%s/pay" % tx, None, cust, {"Idempotency-Key": str(uuid.uuid4())}, note="pay")[1]


def upload(cust, app, t, owner="applications"):
    return call("POST", "/api/%s/%s/documents" % (owner, app), {"doc_type": t, "file_name": "%s_%s.pdf" % (t, cust.lower()),
                                                               "mime_type": "application/pdf", "size_bytes": 200000},
                cust, note="upload " + t)


def officer(app, action, note="E2E officer decision", docs=None):
    _, case = call("GET", "/api/officer/cases/" + app, headers=OFFICER, note="officer opens case")
    return call("POST", "/api/officer/cases/%s/decision" % app, {"action": action, "note": note,
                "expected_version": case["application"]["version"], "requested_documents": docs or ["payslip"]},
                headers=OFFICER, note="officer " + action)


# ---------------------------------------------------------------- scenarios
def t01_auto_approval():
    tx, v = pay_later("CUS-001", "QR-261016-0842")
    expect(v["application"]["credit_state"] == "APPROVED" and v["outcome"] == "APPROVED_TERMS_PENDING", "auto approval")
    expect(not v["application"]["documents"], "no documents requested")
    v = confirm_pay("CUS-001", tx, v)
    expect(v["transaction"]["state"] == "PAYMENT_COMPLETED" and v["loan"], "paid + loan")
    _, loans = call("GET", "/api/loans", cust="CUS-001", note="My Loans")
    expect(loans["loans"][0]["outstanding_vnd"] == 12000000, "loan persisted")
    return "DATA_READY → APPROVED (STP) → terms → PAYMENT_COMPLETED → loan %s in My Loans" % v["loan"]["loan_ref"]


def t02_tc_mandatory():
    tx = scan("CUS-001", "QR-261016-2004", accept=False)
    st, e = call("POST", "/api/transactions/%s/payment-method" % tx, {"method": "PAY_LATER"}, "CUS-001", note="try without T&C")
    expect((st, e["error"]["code"]) == (422, "TC_REQUIRED"), "blocked without T&C")
    st, e = call("POST", "/api/transactions/%s/terms-and-conditions" % tx, {"accepted": False}, "CUS-001", note="unticked box")
    expect(st == 422, "unticked rejected")
    st, v = call("POST", "/api/transactions/%s/terms-and-conditions" % tx, {"accepted": True}, "CUS-001", note="tick box")
    expect(v["outcome"] == "CHECKOUT", "unlocked")
    call("POST", "/api/transactions/%s/cancel" % tx, None, "CUS-001", note="cancel")
    return "Method selection 422 TC_REQUIRED until the single T&C box is ticked"


def t03_amount_range():
    out = []
    for ref in ("QR-261016-1830", "QR-261016-1915"):
        tx = scan("CUS-001", ref)
        st, e = call("POST", "/api/transactions/%s/payment-method" % tx, {"method": "PAY_LATER"}, "CUS-001", note="Pay Later " + ref)
        expect(e["error"]["code"] == "PAY_LATER_OUT_OF_RANGE", "range")
        out.append(e["error"]["details"]["direction"])
        call("POST", "/api/transactions/%s/cancel" % tx, None, "CUS-001", note="cancel")
    return "980K → %s, 95M → %s (Pay Now still offered)" % tuple(out)


def t04_sufficient_profile():
    tx, v = pay_later("CUS-007", "QR-261016-1505")
    expect(v["application"]["credit_state"] == "APPROVED" and not v["application"]["documents"], "no docs")
    confirm_pay("CUS-007", tx, v, 3)
    return "No CIC history, sufficient verified profile → APPROVED without document request"


def t05_poor_profile_docs_officer():
    tx, v = pay_later("CUS-002", "QR-261016-0915")
    a = v["application"]
    expect(a["info_request"]["route"] == "SUPPORTING_DOCUMENTS", "documents requested")
    upload("CUS-002", a["application_id"], "payslip")
    upload("CUS-002", a["application_id"], "bank_statement")
    _, v = call("POST", "/api/applications/%s/information" % a["application_id"], {}, "CUS-002", note="submit documents")
    expect(v["application"]["credit_state"] == "MANUAL_REVIEW", "review, not auto-approved")
    officer(a["application_id"], "request_info", "Statement cropped, re-upload", ["bank_statement"])
    upload("CUS-002", a["application_id"], "bank_statement")
    call("POST", "/api/applications/%s/information" % a["application_id"], {}, "CUS-002", note="resubmit")
    st, case = officer(a["application_id"], "approve", "Income verified (simulated)")
    _, v = call("GET", "/api/transactions/" + tx, cust="CUS-002", note="customer view")
    expect(v["outcome"] == "APPROVED_TERMS_PENDING", "approved after review")
    return "RF-02/RF-03 → documents → MANUAL_REVIEW → officer requests replacement → resubmit → officer approves"


def t06_missing_info():
    tx, v = pay_later("CUS-006", "QR-261016-2110")
    expect(v["application"]["readiness_status"] == "INCOMPLETE", "incomplete")
    _, v = call("POST", "/api/applications/%s/information" % v["application"]["application_id"],
                {"fields": {"customer_profile": "Online merchants (MSME)"}}, "CUS-006", note="provide profile")
    expect(v["application"]["credit_state"] == "APPROVED", "resumed")
    call("POST", "/api/transactions/%s/cancel" % tx, None, "CUS-006", note="customer cancels before confirming")
    return "INCOMPLETE → profile provided → re-assessed → APPROVED (then cancelled by customer)"


def t07_third_party_granted():
    tx, v = pay_later("CUS-005", "QR-261016-1240")
    expect("third_party_consent" in v["application"]["info_request"]["options"], "asked only now")
    _, v = call("POST", "/api/applications/%s/third-party-consent" % v["application"]["application_id"], {"granted": True},
                "CUS-005", note="allow partner check")
    expect(v["application"]["credit_state"] == "APPROVED", "EV-3P")
    call("POST", "/api/transactions/%s/cancel" % tx, None, "CUS-005", note="customer cancels before confirming")
    return "INSUFFICIENT_EVIDENCE → contextual consent → granted → EV-3P → APPROVED (then cancelled by customer)"


def t08_third_party_declined():
    tx, v = pay_later("CUS-005", "QR-261016-1240")
    app = v["application"]["application_id"]
    _, v = call("POST", "/api/applications/%s/third-party-consent" % app, {"granted": False}, "CUS-005", note="decline partner check")
    expect(v["application"]["credit_state"] == "MORE_INFORMATION_REQUIRED" and v["application"]["info_request"]["options"] ==
           ["documents", "manual_review"], "alternatives")
    _, v = call("POST", "/api/applications/%s/manual-review" % app, None, "CUS-005", note="request manual review")
    expect(v["outcome"] == "UNDER_REVIEW", "manual review")
    officer(app, "reject", "Insufficient evidence after review (demo)")
    _, v = call("GET", "/api/transactions/" + tx, cust="CUS-005", note="customer view")
    expect(v["outcome"] == "REJECTED" and not v["payments"], "rejected, no payment")
    return "Declined → not rejected; documents or manual review offered → manual review → officer rejects (no payment)"


def t09_conflict_review_more_info_approve():
    tx, v = pay_later("CUS-003", "QR-261016-1021")
    app = v["application"]["application_id"]
    expect(v["outcome"] == "UNDER_REVIEW", "verify → review")
    officer(app, "request_info", "Please provide a bank statement", ["bank_statement"])
    upload("CUS-003", app, "bank_statement")
    call("POST", "/api/applications/%s/information" % app, {}, "CUS-003", note="submit")
    officer(app, "approve", "Statement confirms inflows (simulated)")
    _, v = call("GET", "/api/transactions/" + tx, cust="CUS-003", note="customer view")
    v = confirm_pay("CUS-003", tx, v, 12)
    expect(v["transaction"]["state"] == "PAYMENT_COMPLETED", "paid")
    return "VERIFY_DATA → officer requests document → upload → officer approves → confirm → paid"


def t10_rapid_drain_reject():
    tx, v = pay_later("CUS-004", "QR-261016-1133")
    app = v["application"]["application_id"]
    upload("CUS-004", app, "payslip")
    upload("CUS-004", app, "bank_statement")
    call("POST", "/api/applications/%s/information" % app, {}, "CUS-004", note="submit")
    officer(app, "reject", "Withdrawal pattern not explained (demo)")
    st, e = call("POST", "/api/transactions/%s/pay" % tx, None, "CUS-004", {"Idempotency-Key": str(uuid.uuid4())}, note="try to pay")
    expect(st == 409, "cannot pay rejected")
    return "R-REV-01 → documents → review → officer rejects → pay attempt 409"


def t11_cancel_and_expiry():
    tx = scan("CUS-001", "QR-261016-2230")
    _, v = call("GET", "/api/transactions/" + tx, cust="CUS-001", note="countdown")
    expect(v["seconds_left"] and v["seconds_left"] > 290, "5-minute timer")
    call("POST", "/api/demo/transactions/%s/expire-now" % tx, None, "CUS-001", note="simulate 5-minute timeout")
    _, v = call("GET", "/api/transactions/" + tx, cust="CUS-001", note="view after expiry")
    expect(v["outcome"] == "EXPIRED", "expired")
    st, e = call("POST", "/api/transactions/%s/payment-method" % tx, {"method": "ACCOUNT"}, "CUS-001", note="try to continue")
    expect(st == 409, "cannot continue")
    tx2 = scan("CUS-001", "QR-261016-2230")
    expect(tx2 != tx, "restart")
    call("POST", "/api/transactions/%s/cancel" % tx2, None, "CUS-001", note="cancel restarted checkout")
    return "5:00 countdown → EXPIRED (cannot continue) → restart creates new checkout → cancel"


def t12_cancel_during_review():
    _, cases = call("GET", "/api/officer/cases", headers=OFFICER)
    tx, v = pay_later("CUS-003", "QR-261016-2004")
    app = v["application"]["application_id"]
    call("POST", "/api/transactions/%s/cancel" % tx, None, "CUS-003", note="customer cancels during review")
    st, e = officer(app, "approve", "late approval")
    expect(st == 409, "cannot approve cancelled")
    return "Cancelled during manual review → officer approval returns 409; no payment possible"


def t13_payment_failure_retry():
    call("POST", "/api/demo/controls", {"key": "force_next_payment_failure", "value": "1"}, headers=OFFICER,
         note="demo control: fail next simulated payment")
    tx, v = pay_later("CUS-007", "QR-261016-2110")
    expect(v["application"]["credit_state"] == "APPROVED", "approved")
    v = confirm_pay("CUS-007", tx, v)
    expect(v["transaction"]["state"] == "PAYMENT_FAILED" and v["application"]["credit_state"] == "APPROVED", "failed, approval kept")
    expect(v["loan"] is None, "no loan on failure")
    _, v = call("POST", "/api/transactions/%s/pay" % tx, None, "CUS-007", {"Idempotency-Key": str(uuid.uuid4())}, note="retry")
    expect(v["transaction"]["state"] == "PAYMENT_COMPLETED", "retry ok")
    return "PAYMENT_FAILED (credit stays APPROVED, no loan) → retry → PAYMENT_COMPLETED"


def t14_pay_loan_reminders_overdue():
    _, loans = call("GET", "/api/loans", cust="CUS-001", note="My Loans")
    ref = next(l for l in loans["loans"] if l["merchant_name"] == "ABC Electronics")["loan_ref"]
    call("POST", "/api/demo/clock", {"date": "2026-11-13"}, note="demo date: 3 days before due")
    _, n = call("GET", "/api/notifications", cust="CUS-001", note="notifications")
    expect(any("due in 3 day" in i["title"] for i in n["items"]), "reminder")
    call("POST", "/api/demo/clock", {"date": "2026-11-16"}, note="demo date: due date")
    _, n = call("GET", "/api/notifications", cust="CUS-001", note="notifications")
    expect(any(i["title"] == "Instalment due today" for i in n["items"]), "due today")
    call("POST", "/api/demo/clock", {"date": "2026-11-17"}, note="demo date: 1 day overdue")
    _, loan = call("GET", "/api/loans/" + ref, cust="CUS-001", note="loan detail")
    expect(loan["overdue_count"] >= 1 and loan["instalments"][0]["late_fee_vnd"] == 0, "overdue, no fee configured")
    _, loan = call("POST", "/api/loans/%s/repay" % ref, {"instalments": [1, 2]}, "CUS-001", {"Idempotency-Key": str(uuid.uuid4())},
                   note="Pay Loan 2 instalments")
    expect(loan["paid_count"] == 2 and loan["outstanding_vnd"] == 8000000 and loan["overdue_count"] == 0, "repaid")
    return "Reminder (−3d) → due today → overdue (late fee 0, RF-01 set) → Pay Loan 2 instalments → outstanding 8M"


def t15_missed_payment_affects_next():
    tx, v = pay_later("CUS-001", "QR-261016-1352")
    expect(v["application"]["info_request"]["route"] == "SUPPORTING_DOCUMENTS", "RF-01 → documents")
    _, case = call("GET", "/api/officer/cases/" + v["application"]["application_id"], headers=OFFICER, note="officer view")
    expect("RF-01" in case["routing"]["rule_ids"], "RF-01")
    call("POST", "/api/transactions/%s/cancel" % tx, None, "CUS-001", note="cancel")
    return "After a missed instalment, the next Pay Later request asks for documents (RF-01), not a decline"


def t16_limit_increase():
    tx = scan("CUS-001", "QR-261016-1610")
    st, e = call("POST", "/api/transactions/%s/payment-method" % tx, {"method": "PAY_LATER"}, "CUS-001", note="Pay Later 30M")
    expect(e["error"]["code"] == "CREDIT_LIMIT_EXCEEDED", "limit exceeded")
    st, r = call("POST", "/api/limit-requests", {"requested_limit_vnd": 40000000, "reason": "Electric scooter", "transaction_id": tx},
                 "CUS-001", note="request increase")
    upload("CUS-001", r["request_id"], "payslip", "limit-requests")
    call("POST", "/api/limit-requests/%s/submit" % r["request_id"], None, "CUS-001", note="submit")
    _, o = call("GET", "/api/officer/limit-requests/" + r["request_id"], headers=OFFICER, note="officer opens")
    call("POST", "/api/officer/limit-requests/%s/decision" % r["request_id"], {"action": "approve", "approved_limit_vnd": 40000000,
         "expected_version": o["version"], "note": "Payslip verified (simulated)"}, headers=OFFICER, note="officer approves limit")
    _, c = call("GET", "/api/credit", cust="CUS-001", note="credit summary")
    expect(c["limit_vnd"] == 40000000 and c["available_vnd"] == 40000000 - c["outstanding_vnd"], "recalculated")
    _, v = call("POST", "/api/transactions/%s/payment-method" % tx, {"method": "PAY_LATER"}, "CUS-001", note="retry Pay Later")
    expect(v["outcome"] == "READY_TO_ASSESS", "now within available credit")
    call("POST", "/api/transactions/%s/cancel" % tx, None, "CUS-001", note="cancel")
    # rejection path
    st, r = call("POST", "/api/limit-requests", {"requested_limit_vnd": 30000000, "reason": "More room"}, "CUS-007", note="request")
    upload("CUS-007", r["request_id"], "employment", "limit-requests")
    call("POST", "/api/limit-requests/%s/submit" % r["request_id"], None, "CUS-007", note="submit")
    _, o = call("GET", "/api/officer/limit-requests/" + r["request_id"], headers=OFFICER, note="officer opens")
    call("POST", "/api/officer/limit-requests/%s/decision" % r["request_id"], {"action": "reject", "expected_version": o["version"],
         "note": "Income not sufficient for requested limit (demo)"}, headers=OFFICER, note="officer rejects limit")
    _, c = call("GET", "/api/credit", cust="CUS-007", note="credit summary")
    expect(c["limit_vnd"] == 10000000, "unchanged after rejection")
    return "30M > available → limit request + payslip → officer approves 40M (available recalculated); 2nd request rejected (limit unchanged)"


def t17_closed_requests_cannot_pay():
    tx, v = pay_later("CUS-007", "QR-261016-2230")
    call("POST", "/api/transactions/%s/cancel" % tx, None, "CUS-007", note="cancel approved-but-unconfirmed")
    st1, _ = call("POST", "/api/applications/%s/terms/confirm" % v["application"]["application_id"], {"tenure_months": 6, "accepted": True},
                  "CUS-007", note="try confirm after cancel")
    st2, e = call("POST", "/api/transactions/%s/pay" % tx, None, "CUS-007", {"Idempotency-Key": str(uuid.uuid4())}, note="try pay after cancel")
    expect(st1 == 409 and st2 == 409 and e["error"]["code"] == "TRANSACTION_CLOSED", "blocked")
    st, e = call("POST", "/api/qr/resolve", {"payload": QR["QR-261016-0842"]}, "CUS-001", note="re-scan paid QR")
    expect(e["error"]["code"] == "QR_ALREADY_PAID", "duplicate blocked")
    return "Cancelled/expired requests: confirm 409, pay 409 TRANSACTION_CLOSED; paid QR re-scan 409"


def t18_pay_now():
    tx = scan("CUS-007", "QR-261016-1722")
    call("POST", "/api/transactions/%s/payment-method" % tx, {"method": "ACCOUNT"}, "CUS-007", note="Pay Now")
    _, v = call("POST", "/api/transactions/%s/pay" % tx, None, "CUS-007", {"Idempotency-Key": str(uuid.uuid4())}, note="pay")
    expect(v["transaction"]["state"] == "PAYMENT_COMPLETED" and v["application"] is None, "account payment")
    return "Pay Now → simulated account debit → PAYMENT_COMPLETED (no credit application)"


def main():
    st, _ = call("POST", "/api/demo/reset", headers=OFFICER, note="reset")
    assert st == 200, "reset failed - is the server running at %s?" % BASE
    LOG.clear()
    for q in call("GET", "/api/demo/qr-codes")[1]:
        QR[q["qr_ref"]] = q["payload"]
    LOG.clear()
    steps = [("01 Eligible QR → automatic approval → loan", t01_auto_approval),
             ("02 Mandatory T&C checkbox", t02_tc_mandatory),
             ("03 Amount below / above Pay Later range", t03_amount_range),
             ("04 Sufficient profile, no unnecessary documents", t04_sufficient_profile),
             ("05 Poor profile → documents → officer more-docs → approve", t05_poor_profile_docs_officer),
             ("06 Missing information → resume", t06_missing_info),
             ("07 Third-party consent only when needed (granted)", t07_third_party_granted),
             ("08 Third-party consent declined → manual review → reject", t08_third_party_declined),
             ("09 Manual review → request info → approve → pay", t09_conflict_review_more_info_approve),
             ("10 Rapid drain → documents → officer rejection", t10_rapid_drain_reject),
             ("11 Checkout countdown, expiry and restart", t11_cancel_and_expiry),
             ("12 Customer cancels during manual review", t12_cancel_during_review),
             ("13 Payment failure then retry", t13_payment_failure_retry),
             ("14 Pay Loan, reminders, due date, overdue", t14_pay_loan_reminders_overdue),
             ("15 Missed payment influences next assessment", t15_missed_payment_affects_next),
             ("16 Credit limit exceeded → increase approved / rejected", t16_limit_increase),
             ("17 Cancelled/expired requests cannot be approved or paid", t17_closed_requests_cannot_pay),
             ("18 Pay Now from account", t18_pay_now)]
    results = []
    for name, fn in steps:
        CURRENT["scenario"] = name[:2]
        t0 = time.time()
        try:
            detail, ok = fn(), True
        except Exception as e:  # report and continue
            detail, ok = "%s: %s" % (type(e).__name__, e), False
        results.append({"id": name[:2], "scenario": name, "passed": ok, "detail": detail, "seconds": round(time.time() - t0, 2)})
        print("%s  %-60s %s" % ("PASS" if ok else "FAIL", name, detail))
    passed = sum(r["passed"] for r in results)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    outdir = os.path.join(ROOT, "docs", "test_logs")
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "e2e_%s.json" % stamp), "w") as f:
        json.dump({"base_url": BASE, "run_at": stamp, "passed": passed, "total": len(results), "results": results, "steps": LOG}, f,
                  indent=2, ensure_ascii=False)
    with open(os.path.join(outdir, "e2e_%s.md" % stamp), "w") as f:
        f.write("# Smart Credit E2E persona test log (%s)\n\nServer: `%s` · Result: **%d/%d scenarios passed** · %d HTTP steps\n\n"
                "Mock-persona walkthroughs driven over the same HTTP API the UI uses. Fictional data; simulated banking. "
                "Audit-event ranges refer to the server's append-only `audit_events` table for this run.\n\n"
                % (stamp, BASE, passed, len(results), len(LOG)))
        f.write("## Summary\n\n| # | Scenario | Result | Evidence |\n|---|---|---|---|\n")
        for r in results:
            f.write("| %s | %s | %s | %s |\n" % (r["id"], r["scenario"][3:], "PASS" if r["passed"] else "**FAIL**", r["detail"]))
        f.write("\n## Step log\n\n| Scn | Actor | Call | HTTP | Result | Audit events | Note |\n|---|---|---|---|---|---|---|\n")
        for s in LOG:
            ev = "#%d–#%d" % tuple(s["audit_events"]) if s["audit_events"] else ""
            f.write("| %s | %s | `%s` | %s | %s | %s | %s |\n" % (s["scenario"], s["actor"], s["call"], s["status"],
                                                                 s.get("result", ""), ev, s["note"]))
    print("\n%d/%d scenarios passed; %d HTTP steps logged → docs/test_logs/e2e_%s.md" % (passed, len(results), len(LOG), stamp))
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
