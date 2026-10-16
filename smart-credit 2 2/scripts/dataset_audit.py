"""Retrospective audit of the supplied HLBVN dataset (stdlib only).

Usage:  python3 scripts/dataset_audit.py [path/to/dataset.xlsx]

Outputs data/reference/dataset_audit.json with:
  * data-quality findings quoted in the brief (re-computed, not copied),
  * recorded process baselines (medians) - reference only, not Smart Credit results,
  * a replay of the Smart Credit data-readiness engine over every row using
    PRE-ASSESSMENT fields only (outcome columns are never passed in),
  * the source rows used to derive the fictional demo personas.
"""
import json
import os
import re
import statistics
import sys
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from server.engine import readiness, routing  # noqa: E402

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
PRE_ASSESSMENT = ["customer_profile", "has_formal_cic_history", "has_observable_tx", "monthly_inflow_vnd", "inflow_cv",
                  "unified_credit_score", "casa_balance_vnd", "dti_ratio", "on_time_bill_ratio", "e_wallet_topups_pm",
                  "regular_weekly_pattern", "rapid_drain_flag"]


def _col_index(ref):
    letters = re.match(r"[A-Z]+", ref).group(0)
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def read_xlsx(path):
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", NS):
                shared.append("".join(t.text or "" for t in si.iter("{%s}t" % NS["m"])))
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        rel_map = {r.get("Id"): r.get("Target") for r in rels}
        sheets = {}
        for s in wb.find("m:sheets", NS):
            rid = s.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
            target = rel_map[rid].lstrip("/")
            target = target if target.startswith("xl/") else "xl/" + target
            rows = []
            for row in ET.fromstring(z.read(target)).iter("{%s}row" % NS["m"]):
                vals = {}
                for c in row.findall("m:c", NS):
                    v = c.find("m:v", NS)
                    t = c.get("t")
                    if t == "s":
                        val = shared[int(v.text)]
                    elif t == "inlineStr":
                        val = "".join(x.text or "" for x in c.iter("{%s}t" % NS["m"]))
                    elif v is None:
                        val = None
                    else:
                        num = float(v.text)
                        val = int(num) if num.is_integer() else num
                    vals[_col_index(c.get("r"))] = val
                if vals:
                    rows.append([vals.get(i) for i in range(max(vals) + 1)])
            sheets[s.get("name")] = rows
        return sheets


def excel_date(v):
    if isinstance(v, (int, float)):
        return (datetime(1899, 12, 30) + timedelta(days=v)).date().isoformat()
    return str(v)[:10]


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "data", "reference", "hlbvn_dataset.xlsx")
    sheets = read_xlsx(src)
    header, *body = sheets["dataset"]
    rows = [dict(zip(header, r)) for r in body]
    policy = json.load(open(os.path.join(ROOT, "server", "policy", "demo_policy.json")))
    n = len(rows)

    tx_dep = policy["transaction_dependent_fields"]["fields"]
    conflict = [r for r in rows if r["has_observable_tx"] == 0 and any((r.get(f) or 0) != 0 for f in tx_dep)]
    rapid = [r for r in rows if r["rapid_drain_flag"] == 1]
    default_no_amount = [r for r in rows if r["is_default_90d"] == 1 and not (r.get("approved_loan_vnd") or 0) > 0]
    bad_tenure = [r for r in rows if r["loan_tenure_months"] not in policy["case_ranges"]["loan_tenure_months"]["allowed"]]
    no_cic = [r for r in rows if r["has_formal_cic_history"] == 0]

    # Replay readiness on pre-assessment fields only. The dataset carries no
    # provenance metadata, so we ASSUME each source is verified and fresh and
    # that consent was given; results are indicative only.
    as_of = date(2026, 10, 16)
    consent = {"granted": True, "categories": policy["consent"]["required_categories"] + ["e_wallet_activity"]}
    status_counts, route_counts = Counter(), Counter()
    for r in rows:
        applicant = {k: r[k] for k in PRE_ASSESSMENT}
        app = {"application_id": "REPLAY", "applicant_id": r["applicant_id"], "qr_ref": "REPLAY", "merchant_id": "REPLAY",
               "requested_loan_vnd": r["requested_loan_vnd"], "loan_tenure_months": r["loan_tenure_months"]}
        observed = excel_date(r["application_date"])
        sources = {
            "hlb_transactions": {"verified": True, "observed_at": as_of.isoformat(), "period_months": 6},
            "credit_information": {"verified": True, "observed_at": as_of.isoformat()},
            "e_wallet_activity": {"verified": True, "observed_at": as_of.isoformat()},
        }
        res = readiness.evaluate(app, applicant, sources, consent, policy, as_of)
        status_counts[res["status"]] += 1
        # No indicative limit exists in the dataset, so routing stops at the
        # STP-candidate stage (AC-03 unresolved -> counted separately).
        dec = routing.decide(res, {"merchant": {"pay_later_eligible": True}, "requested_loan_vnd": r["requested_loan_vnd"],
                                   "loan_tenure_months": r["loan_tenure_months"], "indicative_limit_vnd": None}, policy)
        route_counts["STP_CANDIDATE (approval conditions not evaluable)" if dec["state_path"][0] == "STP_ELIGIBLE"
                     else dec["final_credit_state"]] += 1
        _ = observed

    def med(col):
        return statistics.median(r[col] for r in rows)

    picks = {
        "stp_salaried_cic": next(r for r in rows if r["customer_profile"] == "Salaried, with credit history"
                                 and r["has_observable_tx"] == 1 and r["rapid_drain_flag"] == 0 and r["on_time_bill_ratio"] >= 0.9),
        "no_cic_gig": next(r for r in rows if r["customer_profile"] == "Gig / platform workers" and r["has_formal_cic_history"] == 0
                           and r["has_observable_tx"] == 1 and r["rapid_drain_flag"] == 0),
        "tx_conflict": conflict[0],
        "rapid_drain": next(r for r in rapid if r["has_observable_tx"] == 1),
        "thin_file": next(r for r in conflict[1:] if r["has_formal_cic_history"] == 0),
        "incomplete": next(r for r in rows if r["customer_profile"] == "Online merchants (MSME)" and r["has_observable_tx"] == 1
                           and r["rapid_drain_flag"] == 0),
    }
    out = {
        "source_file": os.path.basename(src),
        "rows": n,
        "columns": header,
        "findings": {
            "has_observable_tx_0_with_populated_tx_fields": len(conflict),
            "rapid_drain_flag_1": len(rapid),
            "is_default_90d_1_with_non_positive_approved_amount": len(default_no_amount),
            "tenure_outside_supported_list": len(bad_tenure),
            "tenure_outside_supported_values": sorted(Counter(r["loan_tenure_months"] for r in bad_tenure).items()),
            "no_formal_cic_history": len(no_cic),
            "requested_loan_vnd_min": min(r["requested_loan_vnd"] for r in rows),
            "requested_loan_vnd_max": max(r["requested_loan_vnd"] for r in rows),
            "unified_credit_score_min": min(r["unified_credit_score"] for r in rows),
            "unified_credit_score_max": max(r["unified_credit_score"] for r in rows),
        },
        "baselines_reference_only": {
            "note": "Recorded in the dataset for the EXISTING processes. Not Smart Credit results; no savings are claimed.",
            "manual_decision_days_median": med("manual_decision_days"),
            "stp_decision_minutes_median": med("stp_decision_minutes"),
            "manual_review_cost_vnd_median": med("manual_review_cost_vnd"),
            "stp_review_cost_vnd_median": med("stp_review_cost_vnd"),
        },
        "readiness_replay": {
            "note": "Data-readiness engine replayed on pre-assessment fields only. Assumes every source verified/fresh and consent "
                    "granted because the dataset has no provenance metadata. Outcome columns are never used. Indicative only.",
            "status_counts": dict(status_counts),
            "route_counts": dict(route_counts),
        },
        "persona_source_rows": {k: {c: (excel_date(v) if c == "application_date" else v) for c, v in r.items()}
                                for k, r in picks.items()},
    }
    dest = os.path.join(ROOT, "data", "reference", "dataset_audit.json")
    with open(dest, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(json.dumps({k: out[k] for k in ("rows", "findings", "baselines_reference_only", "readiness_replay")}, indent=2))
    print("persona rows:", {k: v["applicant_id"] for k, v in out["persona_source_rows"].items()})


if __name__ == "__main__":
    main()
