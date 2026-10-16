# Smart Credit demo API

Base URL `http://127.0.0.1:8080`. JSON in and out. Errors always look like:

```json
{ "error": { "code": "DUPLICATE_PAYMENT", "message": "This purchase has already been paid. No second payment was made.", "details": {} } }
```

| Status | Meaning |
|---|---|
| 200 / 201 | OK / created |
| 400 | malformed JSON |
| 401 | missing demo customer header / officer code |
| 403 | resource belongs to another demo customer |
| 404 | unknown resource or endpoint |
| 409 | invalid state transition, duplicate, stale decision, concurrent update |
| 422 | validation error (bad field, tenure, consent category, missing note…) |

**Demo headers (not real authentication):** `X-Demo-Customer: CUS-001` for customer endpoints. `X-Demo-Officer-Code: DEMO-OFFICER-2026` plus `X-Demo-Officer: <name>` for officer endpoints. `Idempotency-Key: <uuid>` is required on `/pay`.

## Demo catalogue

| Method | Path | Notes |
|---|---|---|
| GET | `/api/health` | |
| GET | `/api/config/policy` | full demo policy with source tags |
| GET | `/api/reference/dataset` | dataset findings, baselines (reference only), readiness replay |
| GET | `/api/demo/customers` · `/api/demo/customers/{id}` | fictional personas |
| GET | `/api/demo/qr-codes` | demo QR payloads (`HLBQR|v1|ref|merchant|amount|sig`), `already_paid` |
| GET | `/api/demo/scenarios` | 14 scenarios |
| POST | `/api/demo/reset` | officer code; re-seeds fixtures |
| POST | `/api/demo/controls` | officer code; `{"key":"force_next_payment_failure","value":"1"}` |

## Customer flow

| Method | Path | Body / notes |
|---|---|---|
| POST | `/api/qr/resolve` | `{"payload": "HLBQR|v1|ref|merchant|amount|sig"}` → 201 new / 200 resumed; 422 `QR_INVALID`; 409 `QR_ALREADY_PAID` / `QR_IN_USE`. Starts the 5-minute timer. |
| POST | `/api/transactions/{tx}/terms-and-conditions` | `{"accepted": true}` (single T&C/Privacy checkbox). Required before any payment method. |
| POST | `/api/transactions/{tx}/payment-method` | `{"method": "PAY_LATER", "tenure_months": 6}` or `{"method": "ACCOUNT"}` (Pay Now). 422 `TC_REQUIRED`, `PAY_LATER_OUT_OF_RANGE` (details.direction), `CREDIT_LIMIT_EXCEEDED` (details.available_vnd, shortfall_vnd). Pay Later creates the application with the T&C as internal-data basis → `READY_TO_ASSESS`. |
| POST | `/api/applications/{app}/assess` | Runs readiness + routing (idempotent). |
| POST | `/api/applications/{app}/documents` | `{"doc_type": "payslip|employment|bank_statement|additional_income", "file_name", "mime_type", "size_bytes"}`. Metadata only; same type replaces the earlier upload. |
| POST | `/api/applications/{app}/information` | `{"fields": {"customer_profile": "…"}}`. Also submits all uploaded documents, then re-assesses. Documents → officer review, never auto-approval. |
| POST | `/api/applications/{app}/third-party-consent` | `{"granted": true|false}`. Only when offered (insufficient evidence). Declining keeps documents/manual review options. |
| POST | `/api/applications/{app}/manual-review` | Customer asks for officer review instead of providing data. |
| GET / POST | `/api/applications/{app}/terms?tenure=12` · `/terms/confirm` | `{"tenure_months": 6, "accepted": true}` |
| POST | `/api/transactions/{tx}/pay` | Header `Idempotency-Key`. 409 `TERMS_NOT_CONFIRMED`, `NOT_APPROVED`, `DUPLICATE_PAYMENT`, `PAYMENT_IN_PROGRESS`, `TRANSACTION_CLOSED` (cancelled/expired), `RETRY_LIMIT`. |
| POST | `/api/transactions/{tx}/cancel` · `/consent/withdraw` | Any time before payment (including during officer review). |
| GET | `/api/transactions` · `/api/transactions/{tx}` · `/api/transactions/{tx}/audit` | History / view / customer-safe timeline. View includes `seconds_left` for the timer. |
| GET | `/api/credit` | `{limit_vnd, outstanding_vnd, available_vnd, internal_delinquency_flag, open_limit_request}` |
| GET | `/api/loans` · `/api/loans/{loan}` | My Loans with instalments (`SCHEDULED/DUE_SOON/DUE_TODAY/OVERDUE/PAID`), repayments, progress. |
| POST | `/api/loans/{loan}/repay` | `{"instalments": [1, 2]}` + `Idempotency-Key`. Oldest-first; 409 `INSUFFICIENT_FUNDS` (recorded), `LOAN_CLOSED`. |
| GET / POST | `/api/notifications` · `/api/notifications/read` | Reminders, overdue alerts, status updates with links. |
| GET / POST | `/api/limit-requests` | Create `{"requested_limit_vnd", "reason", "transaction_id"?}` (status DRAFT). |
| POST | `/api/limit-requests/{id}/documents` · `/submit` · `/cancel` | Submit → PENDING_REVIEW. At least one document needed. |

## Demo controls (fictional environment only)

| Method | Path | |
|---|---|---|
| GET / POST | `/api/demo/clock` | `{"date": "2026-11-17"}` or `{"advance_days": 7}`: simulated business date for due dates/reminders/overdue. |
| POST | `/api/demo/transactions/{tx}/expire-now` | Owner customer or officer; simulates the 5-minute timeout. |

### TransactionView (abridged)
```json
{
  "transaction": { "transaction_id": "TXN-261016-0001", "qr_ref": "QR-261016-0842", "amount_vnd": 12000000,
                   "method": "PAY_LATER", "state": "TERMS_PENDING_CONFIRMATION", "payment_ref": null },
  "merchant": { "name": "ABC Electronics", "pay_later_eligible": true },
  "application": { "application_id": "APP-261016-0001", "credit_state": "APPROVED", "readiness_status": "DATA_READY",
                   "readiness_explanation": "…ready for assessment. This is not an approval.",
                   "decision_message": "Approved. Please review and confirm…", "reasons_for_customer": [],
                   "checks": [{ "name": "Evidence sufficiency", "result": "PASS" }], "terms": { … }, "version": 4 },
  "outcome": "APPROVED_TERMS_PENDING",
  "progress": { "data_ready_for_assessment": true, "credit_approved": true, "payment_completed": false },
  "payments": [], "loan": null, "simulated": true
}
```
`outcome` is one of `TERMS_ACCEPTANCE, CHECKOUT, READY_TO_ASSESS, ASSESSING, UNDER_REVIEW, MORE_INFO_REQUIRED, APPROVED_TERMS_PENDING, PROCESSING, PAYMENT_COMPLETED, PAYMENT_FAILED, POLICY_STOP, REJECTED, CANCELLED, EXPIRED`. The customer view never exposes rule IDs.

## Officer (demo code required)

| Method | Path | |
|---|---|---|
| GET | `/api/officer/cases` | `{pending_review:[…], awaiting_customer:[…], decided:[…]}` |
| GET | `/api/officer/cases/{app}` | full case: readiness (all checks, errors, warnings, gaps, review reasons), routing (rule IDs, approval conditions, state path), input snapshot, sources, consents, decision history, audit log, `can_decide` |
| POST | `/api/officer/cases/{app}/decision` | see below |
| GET | `/api/officer/audit` | latest 300 audit events |
| GET | `/api/officer/limit-requests` · `/api/officer/limit-requests/{id}` | limit-increase queue / detail (documents, profile, audit) |
| POST | `/api/officer/limit-requests/{id}/decision` | `{"action": "approve", "approved_limit_vnd": 30000000, "expected_version": 2}` · reject / request_info need `note`; request_info takes `requested_documents` |

```json
{ "action": "approve | reject | request_info", "note": "required for reject/request_info",
  "expected_version": 3, "requested_documents": ["income_statement"] }
```
Only allowed in `MANUAL_REVIEW` with the transaction still `PENDING_REVIEW`. A mismatched `expected_version` returns **409 STALE_DECISION**. *Approve* sets `APPROVED` and the transaction to `TERMS_PENDING_CONFIRMATION`; it does not pay. *Reject* sets `REJECTED` and the transaction to `CANCELLED` (`CREDIT_REJECTED`). *Request info* sets `MORE_INFORMATION_REQUIRED` while the transaction stays `PENDING_REVIEW`.

## Readiness result schema (engine output)
```json
{ "status": "VERIFY_DATA",
  "validation_errors": [], "verification_warnings": [{ "rule_id": "R-VER-01", "field": "has_observable_tx", "message": "…", "customer_message": "…" }],
  "evidence_gaps": [], "review_reasons": [],
  "checks_performed": [{ "check_id": "C04", "name": "Transaction-observability consistency", "result": "WARN", "detail": "…" }],
  "explanation": "…", "customer_explanation": "…", "missing_fields": [], "missing_consent": [],
  "evidence_paths_met": ["EV-CIC"], "fields_not_used": { "monthly_inflow_vnd": "conflicts with has_observable_tx = 0" },
  "excluded_outcome_fields": [], "unresolved_configuration": [ … ], "policy_version": "2026.10-demo.1" }
```
Rule IDs: `R-INC-01` missing, `R-INV-01` invalid, `R-VER-01` tx conflict, `R-VER-02` unverified/unknown source, `R-VER-03` stale/no period, `R-VER-04` related-field conflict, `R-DOC-01` documents pending officer verification, `R-REV-01` rapid drain, `RF-01` missed instalment (internal), `RF-02/03/04` score / bill-ratio / DTI demo cut-offs (dataset P10/P10/P90), `R-EVD-01` insufficient evidence, `R-CIC-01` (info only), `CUST-MR` customer-requested review, `HS-01/02` hard stops, `AC-01..04` STP approval conditions. Evidence paths: `EV-TX`, `EV-CIC`, `EV-3P` (consented partner data), `EV-DOC` (officer-verified document).
