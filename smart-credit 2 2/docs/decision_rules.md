# Smart Credit: Decision Rules (snapshot of the prototype *before* the 2026-10-16 refinement)

Source: code inspection of `server/engine/readiness.py`, `server/engine/routing.py`, `server/services/smart_credit.py`, `server/policy/demo_policy.json`, plus statistics re-computed from `data/reference/hlbvn_dataset.xlsx` (10,000 rows).

**Basis key:** **E** = threshold/value observed in the dataset · **B** = given in the case brief · **A** = team demo assumption (not evidence, not HLBVN policy) · **H** = human decision.

**Important:** the dataset does **not** feed live decisions. At runtime the engine reads only `demo_policy.json` and the fictional fixtures. The dataset is used (a) offline in `scripts/dataset_audit.py`, and (b) as the source rows for the six demo personas' financial fields.

## A. Decision-rule table

| # | Scenario | Input / condition | Threshold | Decision → next step | Dataset support (10,000 rows) | Basis | Implemented in |
|---|---|---|---|---|---|---|---|
| 1 | Missing mandatory info | Any of application_id, applicant_id, qr_ref, merchant_id, amount, tenure, customer_profile, has_formal_cic_history, has_observable_tx empty | – | `INCOMPLETE` → `MORE_INFORMATION_REQUIRED` → customer supplies → re-assess | 0 nulls in dataset, so the rule never fires on raw data (fixture only) | A | `readiness.evaluate` C02 (L100) · `routing.decide` L54 |
| 2 | Amount out of range | requested_loan_vnd | 1,600,000 – 90,100,000 | `INVALID_DATA` → `MANUAL_REVIEW`; UI/API blocks Pay Later before consent | Observed 1,600,203 – 90,116,228; **1 row above max** | B (≈E) | C03 (L115) · `select_payment_method` L199 (422) |
| 3 | Unsupported tenure | loan_tenure_months | {3,6,9,12,18,24} | `INVALID_DATA` → `MANUAL_REVIEW` | **342 rows (3.4%)** use 4/5/7/8/10/11 mo | B | C03 |
| 4 | Invalid score / ratio / flag | score, on_time_bill_ratio, binary flags, amounts ≥ 0 | 350–850; 0–1; {0,1} | `INVALID_DATA` → `MANUAL_REVIEW` | Score 350–768, bill 0.28–1.00, flags all 0/1 → **0 violations** | B/E (definitions) | C03 |
| 5 | CV / DTI upper bound | inflow_cv, dti_ratio | **none** (UNRESOLVED) | Reported, not enforced | CV 0.045–0.70, DTI 0.12–0.53 | – | `unresolved_configuration` only |
| 6 | Transaction conflict | has_observable_tx = 0 **and** tx fields populated | – | `VERIFY_DATA` (R-VER-01) → `MANUAL_REVIEW`; values kept, marked "not used" | **1,943 rows (19.4%)**; every has_obs = 0 row is affected. Retrospective: manual approval 21.6% vs 50.8%, abandonment 51.2% vs 33.2% | B + E | C04 (L170) |
| 7 | Unverified / unknown source | source record missing or verified = false | – | `VERIFY_DATA` (R-VER-02) → review | **No provenance columns in dataset** | A | C05 (L189) |
| 8 | Stale data | source observed_at age | > 90 days | `VERIFY_DATA` (R-VER-03) → review | No observation-date columns (only application_date Apr–Jun 2026) | A (mirrors 90-day CASA definition) | C06 (L189) |
| 9 | Profile ↔ CIC conflict | "with credit history" ⇒ CIC = 1; "no credit history"/"first-time" ⇒ CIC = 0 | – | `VERIFY_DATA` (R-VER-04) → review | **0 conflicts** (cross-tab is perfectly consistent) | E (labels) | C07 (L235) |
| 10 | Rapid drain | rapid_drain_flag = 1 | = 1 | Review reason R-REV-01 → `MANUAL_REVIEW` (**never auto-decline**) | **266 rows (2.7%)**. Existing STP approved **0%** of them; manual review approved **27.8%** → humans do approve some, which supports review over auto-decline | B + E | C08 (L247) · `routing.decide` |
| 11 | No CIC history | has_formal_cic_history = 0 | – | Info only (R-CIC-01); other evidence paths considered | **6,450 rows (64.5%)**. Manual approval 26.1%, existing STP approval 55.4% → auto-rejecting would exclude most applicants | B + E | C09 (L255) |
| 12 | Insufficient evidence | none of EV-TX (verified tx ≥ 6 mo & has_obs = 1), EV-CIC (verified CIC = 1), EV-DOC (verified document) | 6 months | `INSUFFICIENT_EVIDENCE` (R-EVD-01) → `MANUAL_REVIEW` → officer may request document | In the raw data, no-CIC + has_obs = 0 = 1,943 rows, but all are conflicts (rule 6). A "true thin file" **does not occur**, so the demo persona is adjusted | A | C10 (L263) |
| 13 | Consent missing | required categories hlb_transactions + credit_information not granted, or withdrawn | – | **Policy stop HS-01**, transaction CANCELLED | No consent data in dataset | A | `routing.decide` L43 · `record_consent` L216 · `withdraw_consent` L253 |
| 14 | Ineligible merchant | merchant.pay_later_eligible = false (cash-equivalent) | – | **Policy stop HS-02** | No merchant data | A | `routing.decide` L48 |
| 15 | STP approval | DATA_READY **and** no review reason **and** amount ≤ indicative limit **and** tenure offered | AC-01..04 | `STP_ELIGIBLE` → `APPROVED` → customer confirms terms | Indicative limits are **fixture values** (dataset has no limit column) | A | `routing.decide` L87–92 |
| 16 | Data ready but above limit | AC-03 fails | amount > limit | `STP_ELIGIBLE` → `MANUAL_REVIEW` | – | A | `routing.decide` |
| 17 | Officer decision | MANUAL_REVIEW case, version match | – | Approve → terms; Reject → CANCELLED; Request info → customer | – | H | `officer_decide` L455 (stale → 409) |
| 18 | Terms | Tenure × amount | 0% p.a., VND 0 fee | Equal instalments (remainder on last) | Dataset rates are **15.4–31% (median 20.85%)**, so 0% is **not** evidence-based (taken from the Figma demo) | A | `terms.build_terms` |
| 19 | Payment | approved + terms confirmed + Idempotency-Key | max 2 retries | COMPLETED / FAILED (credit stays APPROVED); duplicate → 409 | – | A | `pay` L537 · `payment_gateway.process` · DB index `ux_payment_success_qr` |
| 20 | Outcome-field leakage | approved amount, rate, default, manual_*, stp_*, abandoned | – | Stripped before assessment | **All 436 default = 1 rows have approved amount = 0**, so the default label is unusable | E | C00 (L77) |

## B. Data-quality findings that shape the design

| Finding | Value | Implication |
|---|---|---|
| has_obs = 0 with tx values | 1,943 (19.4%) | Largest data-quality issue → verification route (rule 6) |
| Unsupported tenures | 342 (3.4%) | Validate tenure before assessment (rule 3) |
| Amount above case max | 1 row (90,116,228) | Brief's range is rounded; edge-case validation |
| No formal CIC | 6,450 (64.5%) | CIC cannot be a hard requirement (rule 11) |
| Rapid drain | 266 (2.7%); STP approved 0%, manual 27.8% | Human review instead of auto-decline (rule 10) |
| Default label | 436 defaults, **all** with approved = 0 | Not a valid training target; no risk model was trained |
| STP vs manual disagreement | 4,128 rows (41.3%) | The existing decision processes diverge heavily; hard to use as ground truth |
| Process baselines | manual 3.24 days / STP 2.01 min; VND 380k / 12k | Reference only; Smart Credit has **not** been measured |

## C. Gaps: rules NOT implemented in this snapshot

| Item | Status |
|---|---|
| Upper bounds for inflow_cv / dti_ratio | UNRESOLVED: reported, not enforced |
| Any use of unified_credit_score, DTI, inflow, CASA, bill ratio in the decision | **Not used** (validated for range only); there is no "poor profile" rule |
| Repayment history / delinquency / overdue / late fees | Not implemented |
| Credit-limit increase requests | Not implemented (above-limit → manual review) |
| Loan repayment ("Pay Loan"), reminders | Not implemented (loan record is created only) |
| Checkout expiry timer | Not implemented |
| Real document upload / third-party consent pop-up | Not implemented (document = simulated checkbox) |
| Dataset-driven thresholds at runtime | None: every runtime threshold is from the brief or a demo assumption |

---

## D. Addendum: rules after the 2026-10-16 refinement (current code)

Sections A–C above describe the prototype **before** the refinement, as requested. The refinement changed or added the rules below. Everything not listed is unchanged.

| # | Scenario | Input / condition | Threshold | Decision → next step | Dataset support | Basis | Implemented in |
|---|---|---|---|---|---|---|---|
| D1 | T&C gate | Single T&C/Privacy checkbox after QR scan | must be `true` | Pay Now / Pay Later blocked (422 `TC_REQUIRED`) until ticked; acceptance is the recorded basis for **internal** data (no separate consent step) | – | A | `checkout.accept_terms_and_conditions`, `select_payment_method`, `_open_application` |
| D2 | Pay Later range | purchase amount | 1,600,000–90,100,000 inclusive | 422 `PAY_LATER_OUT_OF_RANGE` (BELOW_MIN / ABOVE_MAX); Pay Now still offered | 1 row above max | B | `checkout.select_payment_method` |
| D3 | Available credit | amount > limit − outstanding Smart Credit balance | fictional limit | 422 `CREDIT_LIMIT_EXCEEDED` + limit-increase CTA; engine AC-03 also uses available credit | no limit column | A | `select_payment_method`, `servicing.credit_summary`, `routing.decide` AC-03 |
| D4 | Weak score | unified_credit_score < 436 | **dataset P10** | RF-02 → supporting documents → officer | flagged profiles: manual approval 35.2% (1 flag) / 22.4% (2) / 4.3% (3) vs 49.2% (none) | E (distribution, not validated risk) | `readiness` C08, `routing` step 5 |
| D5 | Late bills | on_time_bill_ratio < 0.663 | dataset P10 | RF-03 → documents → officer | as D4 | E | same |
| D6 | High DTI | dti_ratio > 0.3695 | dataset P90 | RF-04 → documents → officer | as D4 | E | same |
| D7 | Missed instalment | internal_delinquency_flag = 1 (set when a Smart Credit instalment goes overdue) | = 1 | RF-01 → documents → officer; **never reported to CIC** | app state, not dataset | A | `servicing.refresh_servicing`, C08 |
| D8 | Rapid drain | rapid_drain_flag = 1 | = 1 | R-REV-01 → documents → officer (still never auto-decline) | 266 rows | B+E | C08 |
| D9 | Documents pending | supporting_documents submitted, not yet officer-verified | – | `VERIFY_DATA` (R-DOC-01) → `MANUAL_REVIEW`; **uploads never auto-approve** | – | A | C05, `checkout.submit_information` |
| D10 | Insufficient evidence | no evidence path (EV-TX / EV-CIC / EV-3P / EV-DOC) | – | Customer chooses: contextual third-party consent (if available) · documents · manual review | true thin file never occurs in raw data | A | `routing` step 4, `checkout.third_party_consent`, `request_manual_review` |
| D11 | Third-party evidence | consented, verified partner e-wallet data ≥ 6 months | 6 months | EV-3P satisfies evidence; partner fields exempt from R-VER-01 | – | A | C04, C10 |
| D12 | Third-party consent declined | customer says no | – | Not rejected: documents or manual review offered | – | A | `third_party_consent` |
| D13 | Checkout timer | customer-side steps unfinished | 300 s | `EXPIRED` (terminal; cannot be approved or paid); paused during officer review/document gathering; new 5-min window after approval | – | A | `base.sweep_expired`, `_move_tx`, `states.TX_EXPIRABLE` |
| D14 | Overdue | instalment unpaid after due date (simulated business date) | due date | OVERDUE + notification; late fee = configured value (**0** in demo) | – | A | `servicing.refresh_servicing` |
| D15 | Reminders | 3 days before / on due date | 3 days | in-app notification (deduplicated) | – | A | same |
| D16 | Limit increase | requested limit > current, ≤ 90.1M | demo max 90,100,000 | separate officer case; approve (≤ requested) / reject / request docs; limit changes only on approval | – | A | `limits.*` |

**Still not implemented / still assumptions:** CV and DTI *validity* upper bounds stay UNRESOLVED. Percentile cut-offs (D4–D6) describe the dataset distribution only; they were **not** validated against outcomes because the default label is unusable. No rule rejects automatically. Interest 0% and late fee 0 are demo values, not HLBVN terms. The dataset still does **not** drive live decisions: the P10/P90 values were computed offline and copied into `demo_policy.json`.

**Replay of the refined engine on the dataset** (pre-assessment fields only; sources assumed verified): DATA_READY 7,890 · VERIFY_DATA 1,767 · INVALID_DATA 343. Routes: 5,954 reach the STP-candidate stage (approval not evaluable without a limit), 1,936 would be asked for documents, 2,110 go to manual review.
