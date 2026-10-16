# HLB Connect Smart Credit: QR-Based Pay Later (MVP)

A clickable, end-to-end prototype for the HLBVN Business Challenge 2026 Strategy Round:

**Scan merchant QR → tick T&C → Pay Now or Pay Later → credit check on internal data → instant approval, supporting documents (or a one-time partner-data check), or credit-officer review → confirm terms within 5 minutes → simulated payment → My Loans with reminders and Pay Loan.**

> **Simulation only.** All customers, merchants, QR codes, documents, terms, payments and repayments are fictional. Nothing connects to real banking systems, CIC, e-wallets or payment rails. The engine is **deterministic and rule-based**, not a trained or validated AI/credit-risk model. Officer access is demo-only and not secure.

## Run it

Requires only Python ≥ 3.9 (standard library, nothing to install).

```bash
cd smart-credit
python3 run.py --reset
```

- Customer app: <http://127.0.0.1:8080/>. Pick a demo customer, or start one of the 16 scenarios from the side panel or the Smart Credit tab.
- Officer workbench: <http://127.0.0.1:8080/officer.html>. Demo code `DEMO-OFFICER-2026`. It has two queues: Credit applications and Limit increases.
- Demo controls: "Demo: expire now" on any checkout timer, demo-date buttons in My Loans / Profile / officer sidebar, "force next payment failure" and "Reset demo data" in the officer sidebar.

## Tests and evidence

```bash
python3 -m unittest discover -s tests -t .              # 81 unit/service/API tests
python3 scripts/e2e_scenarios.py http://127.0.0.1:8080   # 18 persona walkthroughs over HTTP → docs/test_logs/e2e_*.md/.json
python3 scripts/dataset_audit.py                         # dataset findings, baselines, readiness replay
```

- `docs/test_logs/e2e_*.md`: verifiable step log (HTTP call, status, resulting states, audit-event ids) per persona scenario.
- `docs/test_logs/ui_walkthrough_2026-10-16.md`: browser walkthrough of the real UI, including 3 issues found and fixed.
- `docs/peer_testing_kit.md`: task script and record sheet for testing with peers or target borrowers. **Not run with real people yet.**
- `docs/decision_rules.md`: slide-ready decision-rule table with dataset support and code locations.

## What the prototype does

| Area | Behaviour |
|---|---|
| QR & T&C | Server-verified QR (signature + amount). One mandatory checkbox (T&C + Privacy + personal-data handling, with links) before Pay Now / Pay Later. No separate consent for internal data. |
| Limits | Pay Later only for 1,600,000–90,100,000 VND (inclusive) **and** within available credit (fictional limit − outstanding). Otherwise a clear explanation, Pay Now, and a **credit-limit increase request**. |
| Assessment | Internal data only. Readiness: INCOMPLETE / INVALID_DATA / VERIFY_DATA / INSUFFICIENT_EVIDENCE / DATA_READY (not approval). Profile risk flags (score < P10, bills < P10, DTI > P90, missed instalment, rapid drain) → **supporting documents → credit officer**. Never an automatic decline. |
| Documents | Payslip, employment proof, bank statement, other income. Upload (file chooser or sample file), replace, submit, officer can request more. Simulated: metadata only. **Uploads never auto-approve.** |
| Third-party data | Contextual consent pop-up only when evidence is insufficient (e-wallet partner, simulated). Declining offers documents or manual review instead of rejection. |
| Officer | Queues, credit profile & risk indicators with thresholds and basis tags, repayment history, documents, checks, consent records, decision history, audit log. Approve / request documents / reject for applications and limit requests, with stale-version protection. |
| Timer | 5-minute countdown for customer-side steps; expiry is terminal (cannot be approved or paid). No timer while an officer reviews. Cancel at any time before payment. |
| Payment | Approval ≠ paid. Payment only after confirmed terms, idempotent, one success per QR, failure keeps the approval and allows retry. |
| My Loans | Persistent loans, instalment schedule, progress, payment history, **Pay Loan** (oldest first, idempotent). |
| Notifications | Separate inbox: reminders 3 days before and on the due date, overdue alerts (late fee only if configured; 0 in demo), status updates, with links. Missed instalments set an internal flag (not CIC). |
| Audit | Append-only audit for scans, T&C, method, data basis, consents, assessments, routing, documents, officer actions, limit changes, cancellations, expiries, payments, repayments, clock changes. |

## Architecture

```
web/  index.html + js/app.js (customer, mobile)      server/api/http.py            JSON API + static files
      officer.html + js/officer.js (workbench)       server/services/base.py       ids, clocks, state moves, documents, expiry
      css/app.css (Figma + HLB Connect look)         server/services/checkout.py   QR, T&C, method, assessment, documents, consent, terms, pay
      img/hlb-app-icon.png (logo supplied by team)   server/services/officer.py    review queue + decisions
                                                     server/services/servicing.py  loans, Pay Loan, reminders, overdue, notifications
                                                     server/services/limits.py     credit-limit increase cases
                                                     server/engine/                readiness.py · routing.py · terms.py (pure functions)
                                                     server/domain/states.py       credit / transaction / limit-request state machines
                                                     server/store/db.py            SQLite schema, append-only audit triggers
                                                     server/policy/demo_policy.json   every rule tagged DOCUMENTED / DATASET_PERCENTILE / DEMO_ASSUMPTION / UNRESOLVED
                                                     server/fixtures/demo_data.json   7 fictional personas (from dataset rows), merchants, QR codes, scenarios
```

## Demo personas (fictional; financial fields from the named dataset rows)

| ID | Name | Dataset row | Expected behaviour |
|---|---|---|---|
| CUS-001 | Ta My Kim Tien | HLB_POP_100000005 | Sufficient profile → instant approval (limit 18M) |
| CUS-002 | Huynh Gia Bao | HLB_POP_100000020 | Weak profile (score 423, bills 0.59) → documents → officer |
| CUS-003 | Pham Thi Lan | HLB_POP_100000007 | has_observable_tx = 0 with tx values → VERIFY_DATA → officer |
| CUS-004 | Vo Minh Khoa | HLB_POP_100000023 | Rapid drain → documents → officer |
| CUS-005 | Dang Thu Ha | HLB_POP_100000010 (tx fields removed) | Thin file → partner-data consent / documents / manual review |
| CUS-006 | Le Hoang Nam | HLB_POP_100000035 (profile removed) | Missing profile → provide → re-assessed |
| CUS-007 | Nguyen Thanh Truc | HLB_POP_100000070 | No CIC but sufficient profile → instant approval |

## 5-minute demo script

1. **Instant approval → loan → Pay Loan.** Scenario 1. Tick the T&C box, choose Pay Later, and the result is *Approved · Payment not yet*. Confirm & pay, open the loan in My Loans, press "1 day overdue" (overdue, no late fee), then Pay Loan for instalments 1–2.
2. **Weak profile → documents → officer.** Scenario 5. "Supporting documents needed" explains why. Upload two sample files and submit; the case goes to review (not approved). In the officer workbench, look at the risk indicators, then request a replacement document or approve.
3. **Partner-data consent.** Scenario 7. "Let us check partner e-wallet activity" opens the pop-up. *Don't allow* still offers documents or manual review.
4. **Limit increase.** Scenario 12. 30M is above available credit, so request an increase with a payslip. The officer approves a lower limit and available credit is recalculated.
5. **Timer.** Any checkout → "Demo: expire now" → *Checkout expired* (can't be paid).

## Known limitations

- Everything is simulated: camera, CIC, partner API, document checks (metadata only), disbursement, account debits, loan booking and collections. No SMS/email; reminders are in-app.
- Demo-only auth headers for customers and officers. There is no maker-checker or real authorisation model.
- Thresholds are demo assumptions. P10/P90 cut-offs describe the dataset distribution and were **not** validated against outcomes (the default label is unusable). CV and DTI validity bounds remain UNRESOLVED.
- Single-process server with a serialised SQLite connection, suited to a demo and not to load.
- The 5-minute timer uses real time. Loan due dates use a separate simulated business date (demo controls).
- No usability testing with real people has been done yet (kit provided). English UI only.
- The HLB logo image was supplied by the team for this concept prototype. Check brand-usage rules before sharing publicly.

API reference: `docs/API.md`.


## Pay Later servicing assumptions (prototype only)

- Annual simple/flat interest assumption: 12% p.a. on principal over the selected tenure.
- Late fee assumption: VND 50,000 per overdue instalment.
- A loan is marked `DEFAULT` in this demo when an unpaid instalment is more than 90 days overdue. This is an internal simulated status only; it is not a real-world bad-debt classification and nothing is reported to CIC.
- These are configurable illustrative assumptions, not HLBVN product terms, legal advice, or a claim about applicable fees.
- To replay the prototype, open `/officer.html`, connect using the demo officer access code, and select **Reset demo data**. This resets the shared demo for all testers, so coordinate before using it.
