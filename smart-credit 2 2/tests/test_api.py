"""HTTP integration tests against a real server instance on a random port."""
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

os.environ["SMART_CREDIT_QUIET"] = "1"
from server.api.http import DEMO_OFFICER_CODE, make_server  # noqa: E402
from server.main import build_app  # noqa: E402


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.app = build_app(os.path.join(cls.tmp, "test.sqlite3"), payment_delay_ms=0)
        cls.server = make_server(cls.app, "127.0.0.1", 0)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.call("POST", "/api/demo/reset", officer=True)

    def call(self, method, path, body=None, customer=None, officer=False, headers=None):
        h = {"Content-Type": "application/json"}
        if customer:
            h["X-Demo-Customer"] = customer
        if officer:
            h["X-Demo-Officer-Code"] = DEMO_OFFICER_CODE
            h["X-Demo-Officer"] = "officer.test"
        h.update(headers or {})
        data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def payload(self, ref):
        return next(q["payload"] for q in self.call("GET", "/api/demo/qr-codes")[1] if q["qr_ref"] == ref)

    def start(self, cust, ref):
        st, r = self.call("POST", "/api/qr/resolve", {"payload": self.payload(ref)}, customer=cust)
        self.assertEqual(st, 201, r)
        tx = r["view"]["transaction"]["transaction_id"]
        st, err = self.call("POST", "/api/transactions/%s/payment-method" % tx, {"method": "PAY_LATER", "tenure_months": 6},
                            customer=cust)
        self.assertEqual((st, err["error"]["code"]), (422, "TC_REQUIRED"))
        st, _ = self.call("POST", "/api/transactions/%s/terms-and-conditions" % tx, {"accepted": True}, customer=cust)
        self.assertEqual(st, 200)
        st, v = self.call("POST", "/api/transactions/%s/payment-method" % tx, {"method": "PAY_LATER", "tenure_months": 6},
                          customer=cust)
        self.assertEqual((st, v["outcome"]), (200, "READY_TO_ASSESS"))
        st, v = self.call("POST", "/api/applications/%s/assess" % v["application"]["application_id"], customer=cust)
        self.assertEqual(st, 200)
        return tx, v

    def test_static_pages_served(self):
        for path in ("/", "/officer.html"):
            with urllib.request.urlopen(self.base + path) as r:
                self.assertEqual(r.status, 200)
                self.assertIn(b"<html", r.read().lower())

    def test_stp_end_to_end_over_http(self):
        tx, v = self.start("CUS-001", "QR-261016-0842")
        self.assertEqual(v["outcome"], "APPROVED_TERMS_PENDING")
        app_id = v["application"]["application_id"]
        st, t = self.call("GET", "/api/applications/%s/terms?tenure=12" % app_id, customer="CUS-001")
        self.assertEqual((st, t["monthly_instalment_vnd"]), (200, 1120000))
        st, err = self.call("POST", "/api/transactions/%s/pay" % tx, customer="CUS-001", headers={"Idempotency-Key": "a1"})
        self.assertEqual((st, err["error"]["code"]), (409, "TERMS_NOT_CONFIRMED"))
        st, _ = self.call("POST", "/api/applications/%s/terms/confirm" % app_id, {"tenure_months": 6, "accepted": True},
                          customer="CUS-001")
        self.assertEqual(st, 200)
        st, v = self.call("POST", "/api/transactions/%s/pay" % tx, customer="CUS-001", headers={"Idempotency-Key": "a2"})
        self.assertEqual((st, v["transaction"]["state"]), (200, "PAYMENT_COMPLETED"))
        st, v2 = self.call("POST", "/api/transactions/%s/pay" % tx, customer="CUS-001", headers={"Idempotency-Key": "a2"})
        self.assertTrue(v2["idempotent_replay"])
        st, err = self.call("POST", "/api/transactions/%s/pay" % tx, customer="CUS-001", headers={"Idempotency-Key": "a3"})
        self.assertEqual((st, err["error"]["code"]), (409, "DUPLICATE_PAYMENT"))
        st, err = self.call("POST", "/api/qr/resolve", {"payload": self.payload("QR-261016-0842")}, customer="CUS-001")
        self.assertEqual((st, err["error"]["code"]), (409, "QR_ALREADY_PAID"))
        st, hist = self.call("GET", "/api/transactions", customer="CUS-001")
        self.assertEqual(hist[0]["transaction"]["state"], "PAYMENT_COMPLETED")
        st, audit = self.call("GET", "/api/transactions/%s/audit" % tx, customer="CUS-001")
        self.assertIn("PAYMENT_COMPLETED", [e["event_type"] for e in audit])

    def test_officer_flow_and_sync(self):
        tx, v = self.start("CUS-003", "QR-261016-1021")
        self.assertEqual(v["outcome"], "UNDER_REVIEW")
        app_id = v["application"]["application_id"]
        st, err = self.call("GET", "/api/officer/cases")
        self.assertEqual(st, 401)
        st, cases = self.call("GET", "/api/officer/cases", officer=True)
        self.assertEqual(cases["pending_review"][0]["application_id"], app_id)
        st, case = self.call("GET", "/api/officer/cases/" + app_id, officer=True)
        self.assertIn("R-VER-01", case["routing"]["rule_ids"])
        self.assertNotIn("rule_ids", json.dumps(v["application"]), "customer view must not expose rule ids")
        st, err = self.call("POST", "/api/officer/cases/%s/decision" % app_id,
                            {"action": "reject", "note": "x", "expected_version": case["application"]["version"] - 1},
                            officer=True)
        self.assertEqual((st, err["error"]["code"]), (409, "STALE_DECISION"))
        st, case = self.call("POST", "/api/officer/cases/%s/decision" % app_id,
                             {"action": "reject", "note": "Not supported", "expected_version": case["application"]["version"]},
                             officer=True)
        self.assertEqual(st, 200)
        st, cv = self.call("GET", "/api/transactions/" + tx, customer="CUS-003")
        self.assertEqual((cv["outcome"], cv["transaction"]["state"]), ("REJECTED", "CANCELLED"))

    def test_documents_loans_limits_over_http(self):
        tx, v = self.start("CUS-002", "QR-261016-0915")
        app = v["application"]["application_id"]
        for d in ({"doc_type": "payslip", "file_name": "p.pdf", "size_bytes": 10}, {"doc_type": "bank_statement", "file_name": "b.pdf", "size_bytes": 10}):
            st, v = self.call("POST", "/api/applications/%s/documents" % app, d, customer="CUS-002")
            self.assertEqual(st, 200)
        st, v = self.call("POST", "/api/applications/%s/information" % app, {}, customer="CUS-002")
        self.assertEqual((st, v["outcome"]), (200, "UNDER_REVIEW"))
        tx, v = self.start("CUS-001", "QR-261016-0842")
        self.call("POST", "/api/applications/%s/terms/confirm" % v["application"]["application_id"], {"tenure_months": 6, "accepted": True}, customer="CUS-001")
        self.call("POST", "/api/transactions/%s/pay" % tx, customer="CUS-001", headers={"Idempotency-Key": "p1"})
        st, loans = self.call("GET", "/api/loans", customer="CUS-001")
        ref = loans["loans"][0]["loan_ref"]
        st, loan = self.call("POST", "/api/loans/%s/repay" % ref, {"instalments": [1]}, customer="CUS-001", headers={"Idempotency-Key": "r1"})
        self.assertEqual((st, loan["paid_count"]), (200, 1))
        st, clock = self.call("POST", "/api/demo/clock", {"date": "2026-12-17"})
        st, n = self.call("GET", "/api/notifications", customer="CUS-001")
        self.assertTrue(any(i["kind"] == "OVERDUE" for i in n["items"]))
        st, r = self.call("POST", "/api/limit-requests", {"requested_limit_vnd": 30000000, "reason": "x"}, customer="CUS-007")
        self.assertEqual(st, 201)
        self.call("POST", "/api/limit-requests/%s/documents" % r["request_id"], {"doc_type": "payslip", "file_name": "p.pdf"}, customer="CUS-007")
        st, r = self.call("POST", "/api/limit-requests/%s/submit" % r["request_id"], customer="CUS-007")
        st, o = self.call("GET", "/api/officer/limit-requests/" + r["request_id"], officer=True)
        st, o = self.call("POST", "/api/officer/limit-requests/%s/decision" % r["request_id"],
                          {"action": "approve", "approved_limit_vnd": 25000000, "expected_version": o["version"]}, officer=True)
        self.assertEqual((st, o["status"]), (200, "APPROVED"))
        self.assertEqual(self.call("GET", "/api/credit", customer="CUS-007")[1]["limit_vnd"], 25000000)

    def test_errors_and_auth(self):
        self.assertEqual(self.call("POST", "/api/qr/resolve", {"payload": "x"})[0], 401)
        self.assertEqual(self.call("POST", "/api/qr/resolve", {"payload": "garbage"}, customer="CUS-001")[0], 422)
        self.assertEqual(self.call("GET", "/api/transactions/TXN-NOPE", customer="CUS-001")[0], 404)
        self.assertEqual(self.call("GET", "/api/nope")[0], 404)
        self.assertEqual(self.call("GET", "/api/qr/resolve")[0], 405)
        req = urllib.request.Request(self.base + "/api/qr/resolve", data=b"{bad", method="POST",
                                     headers={"X-Demo-Customer": "CUS-001"})
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        self.assertEqual(e.exception.code, 400)
        self.assertEqual(self.call("POST", "/api/demo/reset")[0], 401)
        tx, _ = self.start("CUS-001", "QR-261016-0842")
        self.assertEqual(self.call("GET", "/api/transactions/" + tx, customer="CUS-002")[0], 403)
        st, err = self.call("POST", "/api/transactions/%s/payment-method" % tx, {"method": "PAY_LATER", "tenure_months": "6"},
                            customer="CUS-001")
        self.assertEqual(st, 422)

    def test_reference_baselines(self):
        st, data = self.call("GET", "/api/reference/dataset")
        if st == 404:
            self.skipTest("dataset_audit.json not generated")
        self.assertEqual(data["findings"]["has_observable_tx_0_with_populated_tx_fields"], 1943)
        self.assertEqual(data["findings"]["rapid_drain_flag_1"], 266)
        self.assertEqual(data["findings"]["is_default_90d_1_with_non_positive_approved_amount"], 436)


if __name__ == "__main__":
    unittest.main()


class ResetConcurrencyTests(unittest.TestCase):
    def test_reset_during_traffic_keeps_database_healthy(self):
        tmp = tempfile.mkdtemp()
        try:
            app = build_app(os.path.join(tmp, "r.sqlite3"), payment_delay_ms=0)
            svc = app.service
            payload = svc.db.one("SELECT payload FROM qr_codes WHERE qr_ref = 'QR-261016-1722'")["payload"]
            errors = []

            def traffic():
                for _ in range(40):
                    try:
                        svc.history("CUS-001")
                        svc.list_qr_codes()
                    except Exception as e:  # anything other than a clean read is a failure
                        errors.append(repr(e))

            threads = [threading.Thread(target=traffic) for _ in range(4)]
            [t.start() for t in threads]
            for _ in range(5):
                app.reset()
            [t.join() for t in threads]
            self.assertEqual(errors, [])
            self.assertEqual(svc.db.one("PRAGMA integrity_check")["integrity_check"], "ok")
            r = svc.resolve_qr("CUS-001", payload)
            self.assertFalse(r["resumed"])
            self.assertEqual(svc.db.one("SELECT COUNT(*) n FROM audit_events WHERE event_type = 'DEMO_DATA_SEEDED'")["n"], 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
