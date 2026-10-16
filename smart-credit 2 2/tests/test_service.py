"""Service-level tests for the refined flow: T&C, ranges, profile-based document
requests, third-party consent, officer review, timer/cancel/expiry, payments,
loans, Pay Loan, reminders/overdue, credit-limit increases and audit."""
import json
import os
import sqlite3
import threading
import unittest
from datetime import datetime, timedelta, timezone

from server.domain import states as S
from server.services.smart_credit import ServiceError, SmartCreditService
from server.store.db import open_database, qr_payload

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POLICY = json.load(open(os.path.join(ROOT, "server", "policy", "demo_policy.json")))
FIXTURES = json.load(open(os.path.join(ROOT, "server", "fixtures", "demo_data.json")))
DOC = {"doc_type": "payslip", "file_name": "payslip_sep.pdf", "mime_type": "application/pdf", "size_bytes": 120000}
STMT = {"doc_type": "bank_statement", "file_name": "statement_q3.pdf", "mime_type": "application/pdf", "size_bytes": 300000}


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


def make_service(delay_ms=0):
    clock = Clock()
    return SmartCreditService(open_database(":memory:", FIXTURES), POLICY, FIXTURES, payment_delay_ms=delay_ms, clock=clock), clock


class Base(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock = make_service()
        self.keys = 0

    def payload(self, ref):
        return self.svc.db.one("SELECT payload FROM qr_codes WHERE qr_ref = ?", (ref,))["payload"]

    def scan(self, cust, ref, accept=True):
        tx = self.svc.resolve_qr(cust, self.payload(ref))["view"]["transaction"]["transaction_id"]
        if accept:
            self.svc.accept_terms_and_conditions(tx, cust, True)
        return tx

    def pay_later(self, cust, ref, tenure=6):
        tx = self.scan(cust, ref)
        v = self.svc.select_payment_method(tx, cust, "PAY_LATER", tenure)
        return tx, self.svc.assess(v["application"]["application_id"], cust)

    def key(self):
        self.keys += 1
        return "key-%d" % self.keys

    def confirm_pay(self, cust, tx, v, tenure=6):
        self.svc.confirm_terms(v["application"]["application_id"], cust, tenure, True)
        return self.svc.pay(tx, cust, self.key())

    def events(self, tx):
        return [e["event_type"] for e in self.svc.audit_log(transaction_id=tx)]

    def version(self, app):
        return self.svc.officer_case(app)["application"]["version"]


class CheckoutTests(Base):
    def test_auto_approval_payment_and_loan(self):
        tx, v = self.pay_later("CUS-001", "QR-261016-0842")
        self.assertEqual((v["application"]["readiness_status"], v["application"]["credit_state"], v["outcome"]),
                         ("DATA_READY", "APPROVED", "APPROVED_TERMS_PENDING"))
        self.assertFalse(v["progress"]["payment_completed"])
        self.assertIsNone(v["application"]["info_request"], "no documents for a sufficient profile")
        v = self.confirm_pay("CUS-001", tx, v)
        self.assertEqual(v["transaction"]["state"], "PAYMENT_COMPLETED")
        loans = self.svc.list_loans("CUS-001")
        self.assertEqual(len(loans["loans"]), 1)
        self.assertEqual(loans["loans"][0]["outstanding_vnd"], 12720000)
        self.assertEqual(loans["credit"]["available_vnd"], 18000000 - 12720000)
        ev = self.events(tx)
        for e in ("QR_TRANSACTION_CREATED", "TC_ACCEPTED", "PAYMENT_METHOD_SELECTED", "INTERNAL_DATA_BASIS_RECORDED",
                  "ASSESSMENT_COMPLETED", "ROUTING_DECISION", "TERMS_CONFIRMED", "PAYMENT_COMPLETED"):
            self.assertIn(e, ev)
        self.assertNotIn("CONSENT_REQUESTED", ev, "no separate internal-data consent step")

    def test_tc_checkbox_is_mandatory(self):
        tx = self.scan("CUS-001", "QR-261016-2004", accept=False)
        self.assertEqual(self.svc.get_view(tx, "CUS-001")["outcome"], "TERMS_ACCEPTANCE")
        for m in ("PAY_LATER", "ACCOUNT"):
            with self.assertRaises(ServiceError) as e:
                self.svc.select_payment_method(tx, "CUS-001", m, 6)
            self.assertEqual(e.exception.code, "TC_REQUIRED")
        for bad in (False, None, "true"):
            with self.assertRaises(ServiceError) as e:
                self.svc.accept_terms_and_conditions(tx, "CUS-001", bad)
            self.assertEqual(e.exception.code, "TC_NOT_ACCEPTED")
        v = self.svc.accept_terms_and_conditions(tx, "CUS-001", True)
        self.assertEqual(v["outcome"], "CHECKOUT")
        self.assertEqual(v["transaction"]["tc_version"], POLICY["terms_and_conditions"]["version"])

    def test_amount_range_inclusive_bounds(self):
        for ref, direction in (("QR-261016-1830", "BELOW_MIN"), ("QR-261016-1915", "ABOVE_MAX")):
            tx = self.scan("CUS-001", ref)
            with self.assertRaises(ServiceError) as e:
                self.svc.select_payment_method(tx, "CUS-001", "PAY_LATER", 6)
            self.assertEqual((e.exception.code, e.exception.details["direction"]), ("PAY_LATER_OUT_OF_RANGE", direction))
            self.assertIsNone(self.svc._app_for_tx(tx))
        # inclusive bounds
        for amount in (1600000, 90100000):
            self.svc.db.run("UPDATE customers SET indicative_limit_vnd = 90100000 WHERE customer_id = 'CUS-001'")
            self.svc.db.run("UPDATE qr_codes SET amount_vnd = ?, payload = ? WHERE qr_ref = 'QR-261016-2004'",
                            (amount, qr_payload("QR-261016-2004", "M-NKA", amount)))
            tx = self.scan("CUS-001", "QR-261016-2004")
            v = self.svc.select_payment_method(tx, "CUS-001", "PAY_LATER", 6)
            self.assertEqual(v["outcome"], "READY_TO_ASSESS")
            self.svc.cancel(tx, "CUS-001")

    def test_no_cic_sufficient_profile_no_documents(self):
        tx, v = self.pay_later("CUS-007", "QR-261016-1505")
        self.assertEqual(v["application"]["credit_state"], "APPROVED")
        self.assertEqual(v["application"]["documents"], [])

    def test_poor_profile_documents_then_review_not_auto_approved(self):
        tx, v = self.pay_later("CUS-002", "QR-261016-0915")
        app = v["application"]
        self.assertEqual((app["credit_state"], app["info_request"]["route"]), ("MORE_INFORMATION_REQUIRED", "SUPPORTING_DOCUMENTS"))
        self.assertEqual(sorted(app["info_request"]["options"]), ["documents", "manual_review"])
        with self.assertRaises(ServiceError) as e:
            self.svc.submit_information(app["application_id"], "CUS-002", {})
        self.assertEqual(e.exception.code, "INFORMATION_INCOMPLETE")
        self.svc.upload_application_document(app["application_id"], "CUS-002", DOC)
        with self.assertRaises(ServiceError):
            self.svc.submit_information(app["application_id"], "CUS-002", {})  # bank statement still missing
        self.svc.upload_application_document(app["application_id"], "CUS-002", STMT)
        v = self.svc.submit_information(app["application_id"], "CUS-002", {})
        self.assertEqual((v["application"]["readiness_status"], v["application"]["credit_state"]), ("VERIFY_DATA", "MANUAL_REVIEW"))
        self.assertEqual({d["status"] for d in v["application"]["documents"]}, {"SUBMITTED"})
        self.assertIn("DOCUMENTS_SUBMITTED", self.events(tx))
        case = self.svc.officer_case(app["application_id"])
        self.assertTrue(any(i["rule_id"] == "RF-02" and i["triggered"] for i in case["profile"]["risk_indicators"]))
        self.assertEqual(len(case["documents"]), 2)

    def test_document_replacement_and_officer_request(self):
        tx, v = self.pay_later("CUS-002", "QR-261016-0915")
        app = v["application"]["application_id"]
        self.svc.upload_application_document(app, "CUS-002", DOC)
        self.svc.upload_application_document(app, "CUS-002", dict(DOC, file_name="payslip_v2.pdf"))
        docs = self.svc.get_view(tx, "CUS-002")["application"]["documents"]
        self.assertEqual([d["file_name"] for d in docs], ["payslip_v2.pdf"], "earlier upload replaced")
        self.svc.upload_application_document(app, "CUS-002", STMT)
        self.svc.submit_information(app, "CUS-002", {})
        self.svc.officer_decide(app, "officer.a", "request_info", "Statement is unreadable, please re-upload.",
                                self.version(app), ["bank_statement"])
        cv = self.svc.get_view(tx, "CUS-002")
        self.assertEqual((cv["outcome"], cv["application"]["info_request"]["requested_by"]), ("MORE_INFO_REQUIRED", "Credit officer"))
        with self.assertRaises(ServiceError):
            self.svc.submit_information(app, "CUS-002", {})
        self.svc.upload_application_document(app, "CUS-002", dict(STMT, file_name="statement_clear.pdf"))
        v = self.svc.submit_information(app, "CUS-002", {})
        self.assertEqual(v["application"]["credit_state"], "MANUAL_REVIEW")
        self.svc.officer_decide(app, "officer.a", "approve", "Income verified (simulated).", self.version(app))
        v = self.svc.get_view(tx, "CUS-002")
        self.assertEqual(v["outcome"], "APPROVED_TERMS_PENDING")
        self.assertTrue(all(d["status"] == "ACCEPTED" for d in v["application"]["documents"]))

    def test_missing_info_resumes_to_stp(self):
        tx, v = self.pay_later("CUS-006", "QR-261016-2110")
        self.assertEqual(v["application"]["readiness_status"], "INCOMPLETE")
        v = self.svc.submit_information(v["application"]["application_id"], "CUS-006", {"customer_profile": "Online merchants (MSME)"})
        self.assertEqual(v["application"]["credit_state"], "APPROVED")

    def test_third_party_consent_only_when_needed_and_granted(self):
        tx, v = self.pay_later("CUS-001", "QR-261016-0842")
        self.assertFalse(self.svc.db.one("SELECT 1 FROM consents WHERE kind = 'THIRD_PARTY'"))
        tx, v = self.pay_later("CUS-005", "QR-261016-1240")
        app = v["application"]
        self.assertEqual(app["readiness_status"], "INSUFFICIENT_EVIDENCE")
        self.assertIn("third_party_consent", app["info_request"]["options"])
        v = self.svc.third_party_consent(app["application_id"], "CUS-005", True)
        self.assertEqual((v["application"]["readiness_status"], v["application"]["credit_state"]), ("DATA_READY", "APPROVED"))
        self.assertIn("THIRD_PARTY_CONSENT_GRANTED", self.events(tx))

    def test_third_party_declined_offers_alternatives(self):
        tx, v = self.pay_later("CUS-005", "QR-261016-1240")
        app = v["application"]["application_id"]
        v = self.svc.third_party_consent(app, "CUS-005", False)
        self.assertEqual(v["application"]["credit_state"], "MORE_INFORMATION_REQUIRED", "declining is not a rejection")
        self.assertEqual(v["application"]["info_request"]["options"], ["documents", "manual_review"])
        with self.assertRaises(ServiceError):
            self.svc.third_party_consent(app, "CUS-005", True)  # not offered again for this request
        v = self.svc.request_manual_review(app, "CUS-005")
        self.assertEqual(v["outcome"], "UNDER_REVIEW")
        self.assertIn("CUST-MR", self.svc.officer_case(app)["routing"]["rule_ids"] + [r["rule_id"] for r in self.svc.officer_case(app)["routing"]["reasons"]])

    def test_rapid_drain_documents_then_officer_rejects(self):
        tx, v = self.pay_later("CUS-004", "QR-261016-1133")
        app = v["application"]["application_id"]
        self.svc.upload_application_document(app, "CUS-004", DOC)
        self.svc.upload_application_document(app, "CUS-004", STMT)
        self.svc.submit_information(app, "CUS-004", {})
        self.svc.officer_decide(app, "officer.a", "reject", "Withdrawal pattern not explained (demo).", self.version(app))
        v = self.svc.get_view(tx, "CUS-004")
        self.assertEqual((v["outcome"], v["transaction"]["state"]), ("REJECTED", "CANCELLED"))
        self.assertEqual(self.svc.db.one("SELECT COUNT(*) n FROM payments")["n"], 0)
        with self.assertRaises(ServiceError):
            self.svc.pay(tx, "CUS-004", self.key())

    def test_conflict_review_then_approval_and_payment(self):
        tx, v = self.pay_later("CUS-003", "QR-261016-1021")
        app = v["application"]["application_id"]
        self.assertEqual(v["outcome"], "UNDER_REVIEW")
        self.svc.officer_decide(app, "officer.a", "approve", "Payroll verified (simulated).", self.version(app))
        v = self.svc.get_view(tx, "CUS-003")
        self.assertEqual(v["transaction"]["state"], "TERMS_PENDING_CONFIRMATION")
        self.assertEqual(self.confirm_pay("CUS-003", tx, v, 12)["transaction"]["state"], "PAYMENT_COMPLETED")

    def test_policy_stop(self):
        tx, v = self.pay_later("CUS-001", "QR-261016-1418")
        self.assertEqual((v["outcome"], v["transaction"]["state"]), ("POLICY_STOP", "CANCELLED"))

    def test_payment_failure_then_retry(self):
        tx, v = self.pay_later("CUS-001", "QR-261016-1352")
        v = self.confirm_pay("CUS-001", tx, v)
        self.assertEqual((v["transaction"]["state"], v["application"]["credit_state"]), ("PAYMENT_FAILED", "APPROVED"))
        self.assertIsNone(v["loan"])
        self.assertEqual(self.svc.pay(tx, "CUS-001", self.key())["transaction"]["state"], "PAYMENT_COMPLETED")

    def test_duplicate_protection(self):
        tx, v = self.pay_later("CUS-001", "QR-261016-0842")
        self.svc.confirm_terms(v["application"]["application_id"], "CUS-001", 6, True)
        k = self.key()
        self.svc.pay(tx, "CUS-001", k)
        self.assertTrue(self.svc.pay(tx, "CUS-001", k)["idempotent_replay"])
        with self.assertRaises(ServiceError) as e:
            self.svc.pay(tx, "CUS-001", self.key())
        self.assertEqual(e.exception.code, "DUPLICATE_PAYMENT")
        with self.assertRaises(ServiceError) as e:
            self.svc.resolve_qr("CUS-001", self.payload("QR-261016-0842"))
        self.assertEqual(e.exception.code, "QR_ALREADY_PAID")
        self.assertEqual(self.svc.db.one("SELECT COUNT(*) n FROM loans")["n"], 1)

    def test_pay_now_and_insufficient_funds(self):
        tx = self.scan("CUS-001", "QR-261016-1722")
        self.svc.select_payment_method(tx, "CUS-001", "ACCOUNT")
        v = self.svc.pay(tx, "CUS-001", self.key())
        self.assertEqual((v["transaction"]["state"], v["account"]["balance_vnd"]), ("PAYMENT_COMPLETED", 24850000 - 125000))
        tx = self.scan("CUS-001", "QR-261016-1915")
        self.svc.select_payment_method(tx, "CUS-001", "ACCOUNT")
        v = self.svc.pay(tx, "CUS-001", self.key())
        self.assertEqual(v["payments"][0]["failure_code"], "INSUFFICIENT_FUNDS")


class TimerTests(Base):
    def test_countdown_and_expiry_blocks_later_payment(self):
        tx = self.scan("CUS-001", "QR-261016-2230")
        v = self.svc.get_view(tx, "CUS-001")
        self.assertEqual(v["seconds_left"], 300)
        self.clock.now += timedelta(seconds=301)
        v = self.svc.get_view(tx, "CUS-001")
        self.assertEqual((v["outcome"], v["transaction"]["closure_reason"]), ("EXPIRED", "CHECKOUT_EXPIRED"))
        with self.assertRaises(ServiceError):
            self.svc.select_payment_method(tx, "CUS-001", "ACCOUNT")
        tx2 = self.scan("CUS-001", "QR-261016-2230")
        self.assertNotEqual(tx, tx2, "customer must restart with a new checkout")
        self.assertIn("CHECKOUT_EXPIRED", self.events(tx))

    def test_approved_but_unconfirmed_expires_and_cannot_pay(self):
        tx, v = self.pay_later("CUS-001", "QR-261016-0842")
        self.clock.now += timedelta(seconds=301)
        with self.assertRaises(ServiceError) as e:
            self.svc.confirm_terms(v["application"]["application_id"], "CUS-001", 6, True)
        self.assertEqual(e.exception.code, "INVALID_STATE")
        with self.assertRaises(ServiceError) as e:
            self.svc.pay(tx, "CUS-001", self.key())
        self.assertEqual(e.exception.code, "TRANSACTION_CLOSED")
        self.assertEqual(self.svc.db.one("SELECT COUNT(*) n FROM payments")["n"], 0)

    def test_officer_review_never_expires_and_cancel_blocks_approval(self):
        tx, v = self.pay_later("CUS-003", "QR-261016-1021")
        app = v["application"]["application_id"]
        self.clock.now += timedelta(hours=3)
        self.assertEqual(self.svc.get_view(tx, "CUS-003")["outcome"], "UNDER_REVIEW")
        self.svc.cancel(tx, "CUS-003")
        with self.assertRaises(ServiceError) as e:
            self.svc.officer_decide(app, "officer.a", "approve", None, self.version(app))
        self.assertEqual(e.exception.code, "INVALID_TRANSACTION_STATE")
        self.assertFalse(self.svc.officer_case(app)["can_decide"])

    def test_officer_approval_starts_new_confirmation_window(self):
        tx, v = self.pay_later("CUS-003", "QR-261016-1021")
        app = v["application"]["application_id"]
        self.clock.now += timedelta(minutes=30)
        self.svc.officer_decide(app, "officer.a", "approve", None, self.version(app))
        self.assertEqual(self.svc.get_view(tx, "CUS-003")["seconds_left"], 300)

    def test_expire_now_demo_control_rejects_review_cases(self):
        tx, v = self.pay_later("CUS-003", "QR-261016-1021")
        with self.assertRaises(ServiceError) as e:
            self.svc.expire_now(tx, "tester")
        self.assertEqual(e.exception.code, "NOT_EXPIRABLE")


class LoanTests(Base):
    def _loan(self):
        tx, v = self.pay_later("CUS-001", "QR-261016-0842")
        self.confirm_pay("CUS-001", tx, v)
        return self.svc.list_loans("CUS-001")["loans"][0]["loan_ref"]

    def test_pay_loan_updates_balance_progress_history(self):
        ref = self._loan()
        with self.assertRaises(ServiceError) as e:
            self.svc.repay(ref, "CUS-001", [2], self.key())
        self.assertEqual(e.exception.code, "PAY_OLDEST_FIRST")
        loan = self.svc.repay(ref, "CUS-001", [1, 2], self.key())
        self.assertEqual((loan["paid_count"], loan["outstanding_vnd"]), (2, 8480000))
        self.assertEqual(loan["account"]["balance_vnd"], 24850000 - 4240000)
        self.assertEqual(len(loan["repayments"]), 1)
        self.assertEqual(self.svc.credit_summary("CUS-001")["available_vnd"], 18000000 - 8480000)
        loan = self.svc.repay(ref, "CUS-001", [3, 4, 5, 6], self.key())
        self.assertEqual((loan["status"], loan["outstanding_vnd"]), ("CLOSED", 0))
        with self.assertRaises(ServiceError) as e:
            self.svc.repay(ref, "CUS-001", [1], self.key())
        self.assertEqual(e.exception.code, "LOAN_CLOSED")

    def test_repay_idempotent_and_insufficient_funds_recorded(self):
        ref = self._loan()
        k = self.key()
        self.svc.repay(ref, "CUS-001", [1], k)
        self.assertTrue(self.svc.repay(ref, "CUS-001", [1], k)["idempotent_replay"])
        self.svc.db.run("UPDATE customers SET balance_vnd = 1000 WHERE customer_id = 'CUS-001'")
        with self.assertRaises(ServiceError) as e:
            self.svc.repay(ref, "CUS-001", [2], self.key())
        self.assertEqual(e.exception.code, "INSUFFICIENT_FUNDS")
        self.assertEqual(self.svc.db.one("SELECT COUNT(*) n FROM loan_repayments WHERE status = 'FAILED'")["n"], 1)

    def test_reminders_due_and_overdue_with_internal_flag(self):
        ref = self._loan()
        self.svc.set_business_date("tester", "2026-11-13")
        kinds = [n["title"] for n in self.svc.notifications("CUS-001")["items"]]
        self.assertTrue(any("due in 3 day" in t for t in kinds))
        self.svc.set_business_date("tester", "2026-11-16")
        self.assertTrue(any(n["title"] == "Instalment due today" for n in self.svc.notifications("CUS-001")["items"]))
        self.svc.set_business_date("tester", "2026-11-17")
        loan = self.svc.get_loan(ref, "CUS-001")
        self.assertEqual((loan["instalments"][0]["status"], loan["instalments"][0]["late_fee_vnd"]), ("OVERDUE", 50000))
        self.assertTrue(any(n["kind"] == "OVERDUE" for n in self.svc.notifications("CUS-001")["items"]))
        self.assertEqual(self.svc.credit_summary("CUS-001")["internal_delinquency_flag"], 1)
        # the missed payment now triggers document verification on the next Pay Later request
        tx, v = self.pay_later("CUS-001", "QR-261016-2230")
        self.assertIn("RF-01", self.svc.officer_case(v["application"]["application_id"])["routing"]["rule_ids"])
        self.assertEqual(v["application"]["info_request"]["route"], "SUPPORTING_DOCUMENTS")
        # paying the overdue instalment clears it from the overdue list
        loan = self.svc.repay(ref, "CUS-001", [1], self.key())
        self.assertEqual(loan["overdue_count"], 0)

    def test_configured_late_fee_is_applied(self):
        self.svc.policy = json.loads(json.dumps(POLICY))
        self.svc.policy["repayment_terms"]["annual_interest_rate_pct"] = 0
        self.svc.policy["loan_servicing"]["late_fee_vnd"] = 50000
        ref = self._loan()
        self.svc.set_business_date("tester", "2026-11-20")
        loan = self.svc.get_loan(ref, "CUS-001")
        self.assertEqual(loan["instalments"][0]["total_due_vnd"], 2050000)


class LimitTests(Base):
    def test_limit_exceeded_then_increase_approved(self):
        tx = self.scan("CUS-001", "QR-261016-1610")
        with self.assertRaises(ServiceError) as e:
            self.svc.select_payment_method(tx, "CUS-001", "PAY_LATER", 6)
        self.assertEqual((e.exception.code, e.exception.details["shortfall_vnd"]), ("CREDIT_LIMIT_EXCEEDED", 12000000))
        with self.assertRaises(ServiceError):
            self.svc.create_limit_request("CUS-001", 18000000, "same")  # must be above current
        r = self.svc.create_limit_request("CUS-001", 35000000, "Buying an e-scooter", tx)
        with self.assertRaises(ServiceError) as e:
            self.svc.submit_limit_request(r["request_id"], "CUS-001")
        self.assertEqual(e.exception.code, "DOCUMENTS_REQUIRED")
        self.svc.upload_limit_document(r["request_id"], "CUS-001", DOC)
        r = self.svc.submit_limit_request(r["request_id"], "CUS-001")
        self.assertEqual(r["status"], "PENDING_REVIEW")
        self.assertEqual(self.svc.credit_summary("CUS-001")["limit_vnd"], 18000000, "no change before approval")
        o = self.svc.officer_limit_request(r["request_id"])
        with self.assertRaises(ServiceError):
            self.svc.officer_limit_decide(r["request_id"], "officer.a", "approve", None, o["version"], 40000000)
        self.svc.officer_limit_decide(r["request_id"], "officer.a", "approve", "Income verified", o["version"], 32000000)
        c = self.svc.credit_summary("CUS-001")
        self.assertEqual((c["limit_vnd"], c["available_vnd"]), (32000000, 32000000))
        v = self.svc.select_payment_method(tx, "CUS-001", "PAY_LATER", 6)
        self.assertEqual(v["outcome"], "READY_TO_ASSESS")
        self.assertIn("CREDIT_LIMIT_CHANGED", [e["event_type"] for e in self.svc.audit_log()])

    def test_limit_request_more_info_and_rejection(self):
        r = self.svc.create_limit_request("CUS-001", 30000000, "Planned purchase")
        self.svc.upload_limit_document(r["request_id"], "CUS-001", DOC)
        r = self.svc.submit_limit_request(r["request_id"], "CUS-001")
        o = self.svc.officer_limit_request(r["request_id"])
        self.svc.officer_limit_decide(r["request_id"], "officer.a", "request_info", "Need employment proof", o["version"],
                                      requested_documents=["employment"])
        with self.assertRaises(ServiceError):
            self.svc.submit_limit_request(r["request_id"], "CUS-001")
        self.svc.upload_limit_document(r["request_id"], "CUS-001", {"doc_type": "employment", "file_name": "contract.pdf",
                                                                   "size_bytes": 1000})
        self.svc.submit_limit_request(r["request_id"], "CUS-001")
        o = self.svc.officer_limit_request(r["request_id"])
        with self.assertRaises(ServiceError) as e:
            self.svc.officer_limit_decide(r["request_id"], "officer.b", "reject", "x", o["version"] - 1)
        self.assertEqual(e.exception.code, "STALE_DECISION")
        self.svc.officer_limit_decide(r["request_id"], "officer.a", "reject", "Not supported", o["version"])
        self.assertEqual(self.svc.credit_summary("CUS-001")["limit_vnd"], 18000000)

    def test_available_credit_accounts_for_outstanding_loans(self):
        tx, v = self.pay_later("CUS-001", "QR-261016-0842")
        self.confirm_pay("CUS-001", tx, v)
        tx = self.scan("CUS-001", "QR-261016-1021")  # 9.9M > 6M available
        with self.assertRaises(ServiceError) as e:
            self.svc.select_payment_method(tx, "CUS-001", "PAY_LATER", 6)
        self.assertEqual(e.exception.details["available_vnd"], 5280000)


class GuardTests(Base):
    def test_qr_tamper_and_ownership(self):
        with self.assertRaises(ServiceError) as e:
            self.svc.resolve_qr("CUS-001", qr_payload("QR-261016-0842", "M-ABC", 1000))
        self.assertEqual(e.exception.code, "QR_INVALID")
        tx = self.scan("CUS-001", "QR-261016-0842")
        with self.assertRaises(ServiceError) as e:
            self.svc.get_view(tx, "CUS-002")
        self.assertEqual(e.exception.http_status, 403)
        with self.assertRaises(ServiceError) as e:
            self.svc.resolve_qr("CUS-002", self.payload("QR-261016-0842"))
        self.assertEqual(e.exception.code, "QR_IN_USE")

    def test_document_validation(self):
        tx, v = self.pay_later("CUS-002", "QR-261016-0915")
        app = v["application"]["application_id"]
        for doc, code in (({"doc_type": "selfie", "file_name": "a"}, "INVALID_DOCUMENT"),
                          ({"doc_type": "payslip", "file_name": ""}, "FILE_REQUIRED"),
                          ({"doc_type": "payslip", "file_name": "x", "size_bytes": 50 * 1024 * 1024}, "FILE_TOO_LARGE")):
            with self.assertRaises(ServiceError) as e:
                self.svc.upload_application_document(app, "CUS-002", doc)
            self.assertEqual(e.exception.code, code)
        tx2, v2 = self.pay_later("CUS-001", "QR-261016-2230")  # approved: no documents accepted
        with self.assertRaises(ServiceError):
            self.svc.upload_application_document(v2["application"]["application_id"], "CUS-001", DOC)

    def test_stale_officer_decision(self):
        tx, v = self.pay_later("CUS-003", "QR-261016-1021")
        app = v["application"]["application_id"]
        ver = self.version(app)
        self.svc.officer_decide(app, "officer.a", "approve", None, ver)
        with self.assertRaises(ServiceError) as e:
            self.svc.officer_decide(app, "officer.b", "reject", "late", ver)
        self.assertEqual(e.exception.code, "STALE_DECISION")

    def test_withdraw_consent_during_review(self):
        tx, v = self.pay_later("CUS-003", "QR-261016-1021")
        self.assertEqual(self.svc.withdraw_consent(tx, "CUS-003")["outcome"], "POLICY_STOP")

    def test_assessment_idempotent(self):
        tx, v = self.pay_later("CUS-001", "QR-261016-0842")
        self.assertTrue(self.svc.assess(v["application"]["application_id"], "CUS-001")["already_assessed"])

    def test_retry_limit(self):
        self.svc.db.run("UPDATE qr_codes SET simulated_failures = 9 WHERE qr_ref = 'QR-261016-1722'")
        tx = self.scan("CUS-001", "QR-261016-1722")
        self.svc.select_payment_method(tx, "CUS-001", "ACCOUNT")
        for _ in range(3):
            self.svc.pay(tx, "CUS-001", self.key())
        with self.assertRaises(ServiceError) as e:
            self.svc.pay(tx, "CUS-001", self.key())
        self.assertEqual(e.exception.code, "RETRY_LIMIT")

    def test_notifications_are_synchronised(self):
        tx, v = self.pay_later("CUS-003", "QR-261016-1021")
        app = v["application"]["application_id"]
        self.svc.officer_decide(app, "officer.a", "reject", "No", self.version(app))
        n = self.svc.notifications("CUS-003")
        self.assertEqual([i["title"] for i in n["items"]][:2], ["Pay Later not approved", "Additional review required"])
        self.assertEqual(self.svc.mark_notifications_read("CUS-003")["unread"], 0)


class ConcurrencyTests(unittest.TestCase):
    def test_parallel_pay_requests_produce_one_payment(self):
        svc, _ = make_service(delay_ms=150)
        payload = svc.db.one("SELECT payload FROM qr_codes WHERE qr_ref = 'QR-261016-0842'")["payload"]
        tx = svc.resolve_qr("CUS-001", payload)["view"]["transaction"]["transaction_id"]
        svc.accept_terms_and_conditions(tx, "CUS-001", True)
        v = svc.select_payment_method(tx, "CUS-001", "PAY_LATER", 6)
        app = svc.assess(v["application"]["application_id"], "CUS-001")["application"]["application_id"]
        svc.confirm_terms(app, "CUS-001", 6, True)
        results, errors = [], []

        def worker(i):
            try:
                results.append(svc.pay(tx, "CUS-001", "parallel-%d" % i)["transaction"]["state"])
            except ServiceError as e:
                errors.append(e.code)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(results, ["PAYMENT_COMPLETED"])
        self.assertTrue(set(errors) <= {"PAYMENT_IN_PROGRESS", "DUPLICATE_PAYMENT"}, errors)
        self.assertEqual(svc.db.one("SELECT COUNT(*) n FROM loans")["n"], 1)


class StateMachineTests(unittest.TestCase):
    def test_no_shortcut_to_completion(self):
        for s in S.TX_TRANSITIONS:
            if s != "DISBURSEMENT_PROCESSING":
                self.assertNotIn("PAYMENT_COMPLETED", S.TX_TRANSITIONS[s], s)

    def test_expired_and_cancelled_are_terminal(self):
        for s in ("EXPIRED", "CANCELLED"):
            self.assertEqual(S.TX_TRANSITIONS[s], set())
        self.assertNotIn("PENDING_REVIEW", S.TX_EXPIRABLE)
        self.assertNotIn("DISBURSEMENT_PROCESSING", S.TX_EXPIRABLE)

    def test_engine_cannot_reject(self):
        with self.assertRaises(S.InvalidTransition):
            S.check_credit("ASSESSMENT_PENDING", "REJECTED")

    def test_audit_log_is_append_only(self):
        svc, _ = make_service()
        with self.assertRaises(sqlite3.DatabaseError):
            svc.db.run("UPDATE audit_events SET event_type = 'X'")
        with self.assertRaises(sqlite3.DatabaseError):
            svc.db.run("DELETE FROM audit_events")


if __name__ == "__main__":
    unittest.main()
