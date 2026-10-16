"""Unit tests for the data-readiness engine, routing and terms (pure functions)."""
import copy
import json
import os
import unittest
from datetime import date

from server.engine import readiness as R
from server.engine import routing, terms as T

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POLICY = json.load(open(os.path.join(ROOT, "server", "policy", "demo_policy.json")))
AS_OF = date(2026, 10, 16)
ALL_CONSENT = {"granted": True, "categories": ["hlb_transactions", "credit_information", "e_wallet_activity"]}


def base_case():
    application = {"application_id": "APP-1", "applicant_id": "CUS-1", "qr_ref": "QR-1", "merchant_id": "M-1",
                   "requested_loan_vnd": 12000000, "loan_tenure_months": 6}
    applicant = {"customer_profile": "Salaried, with credit history", "has_formal_cic_history": 1, "has_observable_tx": 1,
                 "monthly_inflow_vnd": 16969073, "inflow_cv": 0.0681, "unified_credit_score": 681,
                 "casa_balance_vnd": 7096492, "dti_ratio": 0.3145, "on_time_bill_ratio": 0.9715, "e_wallet_topups_pm": 6,
                 "regular_weekly_pattern": 1, "rapid_drain_flag": 0}
    sources = {"hlb_transactions": {"verified": True, "observed_at": "2026-10-15", "period_months": 6},
               "credit_information": {"verified": True, "observed_at": "2026-10-15"},
               "e_wallet_activity": {"verified": True, "observed_at": "2026-10-14"}}
    return application, applicant, sources, copy.deepcopy(ALL_CONSENT)


def run(application, applicant, sources, consent, policy=POLICY):
    return R.evaluate(application, applicant, sources, consent, policy, AS_OF)


def route(res, amount=12000000, limit=18000000, tenure=6, eligible=True, third_party=False):
    return routing.decide(res, {"merchant": {"pay_later_eligible": eligible, "category": "x"},
                                "requested_loan_vnd": amount, "loan_tenure_months": tenure,
                                "available_credit_vnd": limit, "third_party_available": third_party}, POLICY)


class ReadinessTests(unittest.TestCase):
    def test_valid_case_is_data_ready(self):
        res = run(*base_case())
        self.assertEqual(res["status"], R.DATA_READY)
        for key in ("status", "validation_errors", "verification_warnings", "evidence_gaps", "review_reasons",
                    "checks_performed", "explanation"):
            self.assertIn(key, res)
        self.assertIn("not a credit approval", res["explanation"])

    def test_missing_mandatory_application_field_is_incomplete(self):
        a, p, s, c = base_case()
        a["qr_ref"] = None
        res = run(a, p, s, c)
        self.assertEqual(res["status"], R.INCOMPLETE)
        self.assertIn("qr_ref", res["missing_fields"])

    def test_missing_profile_is_incomplete(self):
        a, p, s, c = base_case()
        p["customer_profile"] = ""
        self.assertEqual(run(a, p, s, c)["status"], R.INCOMPLETE)

    def test_amount_bounds(self):
        for amount, expected in [(1600000, R.DATA_READY), (90100000, R.DATA_READY),
                                 (1599999, R.INVALID_DATA), (90100001, R.INVALID_DATA), (12000000.5, R.INVALID_DATA)]:
            a, p, s, c = base_case()
            a["requested_loan_vnd"] = amount
            self.assertEqual(run(a, p, s, c)["status"], expected, amount)

    def test_tenure_must_be_supported(self):
        for tenure in (3, 6, 9, 12, 18, 24):
            a, p, s, c = base_case()
            a["loan_tenure_months"] = tenure
            self.assertEqual(run(a, p, s, c)["status"], R.DATA_READY)
        for tenure in (4, 5, 7, 8, 10, 11, 36, 0):
            a, p, s, c = base_case()
            a["loan_tenure_months"] = tenure
            self.assertEqual(run(a, p, s, c)["status"], R.INVALID_DATA, tenure)

    def test_score_ratio_binary_ranges(self):
        cases = [("unified_credit_score", 349), ("unified_credit_score", 851), ("on_time_bill_ratio", 1.01),
                 ("on_time_bill_ratio", -0.1), ("rapid_drain_flag", 2), ("has_observable_tx", "yes"),
                 ("monthly_inflow_vnd", -5), ("customer_profile", "Astronaut")]
        for field, value in cases:
            a, p, s, c = base_case()
            p[field] = value
            res = run(a, p, s, c)
            self.assertEqual(res["status"], R.INVALID_DATA, (field, value))
            self.assertTrue(any(e["field"] == field for e in res["validation_errors"]))

    def test_observable_tx_zero_with_values_requires_verification_and_keeps_values(self):
        a, p, s, c = base_case()
        p["has_observable_tx"] = 0
        res = run(a, p, s, c)
        self.assertEqual(res["status"], R.VERIFY_DATA)
        self.assertTrue(any(w["rule_id"] == "R-VER-01" for w in res["verification_warnings"]))
        self.assertEqual(p["monthly_inflow_vnd"], 16969073, "input must not be erased")
        self.assertIn("monthly_inflow_vnd", res["fields_not_used"])

    def test_unverified_stale_unknown_sources(self):
        a, p, s, c = base_case()
        s["credit_information"]["verified"] = False
        self.assertEqual(run(a, p, s, c)["status"], R.VERIFY_DATA)
        a, p, s, c = base_case()
        s["hlb_transactions"]["observed_at"] = "2026-06-01"
        res = run(a, p, s, c)
        self.assertEqual(res["status"], R.VERIFY_DATA)
        self.assertTrue(any(w["rule_id"] == "R-VER-03" for w in res["verification_warnings"]))
        a, p, s, c = base_case()
        del s["e_wallet_activity"]
        res = run(a, p, s, c)
        self.assertEqual(res["status"], R.VERIFY_DATA)
        self.assertTrue(any("unknown provenance" in w["message"] for w in res["verification_warnings"]))
        a, p, s, c = base_case()
        s["hlb_transactions"]["period_months"] = None
        self.assertEqual(run(a, p, s, c)["status"], R.VERIFY_DATA)

    def test_profile_conflict_requires_verification(self):
        a, p, s, c = base_case()
        p["has_formal_cic_history"] = 0  # profile says "with credit history"
        res = run(a, p, s, c)
        self.assertEqual(res["status"], R.VERIFY_DATA)
        self.assertTrue(any(w["rule_id"] == "R-VER-04" for w in res["verification_warnings"]))

    def test_rapid_drain_adds_review_reason_only(self):
        a, p, s, c = base_case()
        p["rapid_drain_flag"] = 1
        res = run(a, p, s, c)
        self.assertEqual(res["status"], R.DATA_READY)
        self.assertEqual([r["rule_id"] for r in res["review_reasons"]], ["R-REV-01"])

    def test_insufficient_evidence(self):
        a, p, s, c = base_case()
        p.update({"customer_profile": "Salaried, no credit history", "has_formal_cic_history": 0, "has_observable_tx": 0})
        for f in POLICY["transaction_dependent_fields"]["fields"]:
            p[f] = None
        res = run(a, p, s, c)
        self.assertEqual(res["status"], R.INSUFFICIENT_EVIDENCE)
        self.assertEqual(res["evidence_gaps"][0]["rule_id"], "R-EVD-01")

    def test_officer_verified_document_satisfies_evidence(self):
        a, p, s, c = base_case()
        p.update({"customer_profile": "Salaried, no credit history", "has_formal_cic_history": 0, "has_observable_tx": 0})
        for f in POLICY["transaction_dependent_fields"]["fields"]:
            p[f] = None
        s["supporting_documents"] = {"verified": True, "observed_at": "2026-10-16"}
        c["categories"].append("supporting_documents")
        res = run(a, p, s, c)
        self.assertEqual(res["status"], R.DATA_READY)
        self.assertEqual(res["evidence_paths_met"], ["EV-DOC"])

    def test_short_observation_period_is_not_evidence(self):
        a, p, s, c = base_case()
        p.update({"customer_profile": "Gig / platform workers", "has_formal_cic_history": 0})
        s["hlb_transactions"]["period_months"] = 3
        self.assertEqual(run(a, p, s, c)["status"], R.INSUFFICIENT_EVIDENCE)

    def test_no_cic_with_verified_transactions_is_ready(self):
        a, p, s, c = base_case()
        p.update({"customer_profile": "Gig / platform workers", "has_formal_cic_history": 0})
        res = run(a, p, s, c)
        self.assertEqual(res["status"], R.DATA_READY)
        self.assertTrue(any(ch["check_id"] == "C09" and "Not used as a rejection reason" in ch["detail"]
                            for ch in res["checks_performed"]))

    def test_missing_consent_category_drops_source_data(self):
        a, p, s, c = base_case()
        c["categories"] = ["hlb_transactions"]
        res = run(a, p, s, c)
        self.assertEqual(res["missing_consent"], ["credit_information"])
        self.assertIn("unified_credit_score", res["fields_not_used"])

    def test_outcome_fields_are_excluded(self):
        a, p, s, c = base_case()
        p.update({"is_default_90d": 1, "approved_loan_vnd": 0, "stp_approval_status": 0, "manual_decision_days": 3.2})
        res = run(a, p, s, c)
        self.assertEqual(res["status"], R.DATA_READY)
        self.assertEqual(sorted(res["excluded_outcome_fields"]),
                         sorted(["is_default_90d", "approved_loan_vnd", "stp_approval_status", "manual_decision_days"]))

    def test_engine_is_deterministic(self):
        self.assertEqual(run(*base_case()), run(*base_case()))

    def test_status_precedence(self):
        a, p, s, c = base_case()
        a["merchant_id"] = None
        p["unified_credit_score"] = 900
        p["has_observable_tx"] = 0
        self.assertEqual(run(a, p, s, c)["status"], R.INCOMPLETE)


class RoutingTests(unittest.TestCase):
    def test_stp_requires_all_conditions(self):
        dec = route(run(*base_case()))
        self.assertEqual(dec["route"], routing.ROUTE_STP)
        self.assertEqual(dec["state_path"], ["STP_ELIGIBLE", "APPROVED"])
        self.assertTrue(all(c["passed"] for c in dec["approval_conditions"]))

    def test_data_ready_above_limit_is_not_approved(self):
        dec = route(run(*base_case()), amount=30000000, limit=18000000)
        self.assertEqual(dec["state_path"], ["STP_ELIGIBLE", "MANUAL_REVIEW"])
        self.assertIn("AC-03", dec["rule_ids"])

    def test_unresolved_limit_routes_to_review(self):
        dec = route(run(*base_case()), limit=None)
        self.assertEqual(dec["final_credit_state"], "MANUAL_REVIEW")

    def test_rapid_drain_requests_documents_not_rejection(self):
        a, p, s, c = base_case()
        p["rapid_drain_flag"] = 1
        dec = route(run(a, p, s, c))
        self.assertEqual(dec["route"], routing.ROUTE_DOCUMENTS)
        self.assertEqual(dec["final_credit_state"], "MORE_INFORMATION_REQUIRED")
        self.assertIn("R-REV-01", dec["rule_ids"])
        self.assertIn("manual_review", [o for i in dec["requested_items"] if i["type"] == "evidence_options" for o in i["options"]])

    def test_poor_profile_flags_request_documents(self):
        for field, value, rid in (("unified_credit_score", 435, "RF-02"), ("on_time_bill_ratio", 0.66, "RF-03"),
                                  ("dti_ratio", 0.37, "RF-04"), ("internal_delinquency_flag", 1, "RF-01")):
            a, p, s, c = base_case()
            p[field] = value
            res = run(a, p, s, c)
            self.assertEqual(res["status"], R.DATA_READY, field)
            dec = route(res)
            self.assertEqual((dec["route"], dec["rule_ids"]), (routing.ROUTE_DOCUMENTS, [rid]), field)

    def test_profile_flag_boundaries_do_not_trigger(self):
        a, p, s, c = base_case()
        p.update({"unified_credit_score": 436, "on_time_bill_ratio": 0.663, "dti_ratio": 0.3695})
        self.assertEqual(route(run(a, p, s, c))["route"], routing.ROUTE_STP)

    def test_submitted_documents_pending_officer_force_review(self):
        a, p, s, c = base_case()
        p["unified_credit_score"] = 400
        s["supporting_documents"] = {"verified": False, "observed_at": "2026-10-16", "documents": ["payslip"]}
        c["categories"].append("supporting_documents")
        res = run(a, p, s, c)
        self.assertEqual(res["status"], R.VERIFY_DATA)
        self.assertTrue(any(w["rule_id"] == "R-DOC-01" for w in res["verification_warnings"]))
        self.assertEqual(route(res)["final_credit_state"], "MANUAL_REVIEW")

    def test_verify_and_invalid_route_to_review(self):
        for mutate in (lambda a, p, s: p.update(has_observable_tx=0),
                       lambda a, p, s: p.update(on_time_bill_ratio=3)):
            a, p, s, c = base_case()
            mutate(a, p, s)
            self.assertEqual(route(run(a, p, s, c))["final_credit_state"], "MANUAL_REVIEW")

    def test_insufficient_evidence_offers_customer_options(self):
        a, p, s, c = base_case()
        p.update({"customer_profile": "Salaried, no credit history", "has_formal_cic_history": 0, "has_observable_tx": 0})
        for f in POLICY["transaction_dependent_fields"]["fields"]:
            p[f] = None
        for tp, expected in ((True, ["third_party_consent", "documents", "manual_review"]), (False, ["documents", "manual_review"])):
            dec = route(run(a, p, s, c), third_party=tp)
            self.assertEqual(dec["route"], routing.ROUTE_EVIDENCE)
            self.assertEqual(dec["requested_items"][0]["options"], expected)

    def test_third_party_data_satisfies_evidence_only_with_consent(self):
        a, p, s, c = base_case()
        p.update({"customer_profile": "Salaried, no credit history", "has_formal_cic_history": 0, "has_observable_tx": 0})
        for f in POLICY["transaction_dependent_fields"]["fields"]:
            p[f] = None
        p["e_wallet_topups_pm"] = 12
        s["e_wallet_activity"] = {"verified": True, "observed_at": "2026-10-15", "period_months": 6}
        c["categories"] = ["hlb_transactions", "credit_information"]
        self.assertEqual(run(a, dict(p), s, c)["status"], R.INSUFFICIENT_EVIDENCE)
        c["categories"].append("e_wallet_activity")
        res = run(a, dict(p), s, c)
        self.assertEqual((res["status"], res["evidence_paths_met"]), (R.DATA_READY, ["EV-3P"]))

    def test_incomplete_requests_information(self):
        a, p, s, c = base_case()
        p["customer_profile"] = None
        dec = route(run(a, p, s, c))
        self.assertEqual(dec["final_credit_state"], "MORE_INFORMATION_REQUIRED")
        self.assertEqual(dec["requested_items"], [{"field": "customer_profile", "type": "field"}])

    def test_hard_stops(self):
        a, p, s, c = base_case()
        c["categories"] = ["hlb_transactions"]
        dec = route(run(a, p, s, c))
        self.assertEqual((dec["route"], dec["rule_ids"]), (routing.ROUTE_POLICY_STOP, ["HS-01"]))
        dec = route(run(*base_case()), eligible=False)
        self.assertEqual((dec["route"], dec["rule_ids"]), (routing.ROUTE_POLICY_STOP, ["HS-02"]))

    def test_engine_never_rejects(self):
        """Exhaustively vary risky inputs: the engine must never emit REJECTED."""
        for cic in (0, 1):
            for obs in (0, 1):
                for drain in (0, 1):
                    for score in (350, 850, None):
                        a, p, s, c = base_case()
                        p.update({"has_formal_cic_history": cic, "has_observable_tx": obs, "rapid_drain_flag": drain,
                                  "unified_credit_score": score, "customer_profile": "Gig / platform workers"})
                        dec = route(run(a, p, s, c))
                        self.assertNotEqual(dec["final_credit_state"], "REJECTED")
                        if cic == 0:
                            self.assertNotEqual(dec["route"], routing.ROUTE_POLICY_STOP)


class TermsTests(unittest.TestCase):
    def test_zero_interest_schedule(self):
        pol = copy.deepcopy(POLICY)
        pol["repayment_terms"]["annual_interest_rate_pct"] = 0
        t = T.build_terms(12000000, 6, AS_OF, pol)
        self.assertEqual(t["monthly_instalment_vnd"], 2000000)
        self.assertEqual(sum(i["amount_vnd"] for i in t["instalments"]), t["total_repayable_vnd"])
        self.assertEqual(t["first_due_date"], "2026-11-16")
        self.assertEqual(t["last_due_date"], "2027-04-16")

    def test_rounding_remainder_on_last_instalment(self):
        pol = copy.deepcopy(POLICY)
        pol["repayment_terms"]["annual_interest_rate_pct"] = 0
        t = T.build_terms(8500000, 9, AS_OF, pol)
        self.assertEqual(sum(i["amount_vnd"] for i in t["instalments"]), 8500000)
        self.assertEqual(len(t["instalments"]), 9)

    def test_configured_rate_is_applied_consistently(self):
        pol = copy.deepcopy(POLICY)
        pol["repayment_terms"]["annual_interest_rate_pct"] = 12
        t = T.build_terms(12000000, 12, AS_OF, pol)
        self.assertEqual(t["total_interest_vnd"], 1440000)
        self.assertEqual(sum(i["amount_vnd"] for i in t["instalments"]), 13440000)

    def test_unsupported_tenure(self):
        with self.assertRaises(ValueError):
            T.build_terms(12000000, 7, AS_OF, POLICY)


if __name__ == "__main__":
    unittest.main()
