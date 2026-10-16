# UI walkthrough log (2026-10-16)

Who: the build agent (Claude), driving the real app in a browser (mobile viewport 375×812 for the customer app, 1440×900 for the officer workbench) against the local server. This is **not** a test with real customers or peers. See `docs/peer_testing_kit.md` for that.

| # | Persona | Path | Observed | Result |
|---|---|---|---|---|
| U1 | – | Sign-in | HLB Connect welcome look: sky gradient, skyline, HLB logo, customer selector + biometric button, QR Pay / Pay Loan / Smart Credit quick actions | PASS |
| U2 | CUS-001 | Home | Logo header, bell, 8 quick actions (QR Pay, Pay Loan, My Loans, Alerts, History, Limit, Smart Credit, Transfer n/a), available-credit card | PASS |
| U3 | CUS-001 | Scan → ABC 12M | Step 1 screen: purchase card, 5:00 countdown, single T&C/Privacy checkbox, Continue disabled until ticked | PASS |
| U4 | CUS-001 | T&C link | Opens the demo T&C sheet. **Bug found:** the link sat inside the checkbox label, so opening it ticked the box. **Fixed** (links moved out of the activation area) and re-verified: checkbox stays unticked | FIXED → PASS |
| U5 | CUS-001 | Checkout | Pay Now / Pay Later options, tenure chips, demo-terms note, countdown | PASS |
| U6 | CUS-001 | Pay Later | Assessment animation → "Financing approved in demo", new 5:00 window, milestones Data ready / Approved / Not yet | PASS |
| U7 | CUS-001 | Confirm & pay | Payment completed, loan ref LN-261016-0001, VND 0 account debit | PASS |
| U8 | CUS-001 | My Loans → demo date "1 day overdue" | Instalment 1 OVERDUE, "No late fee is configured", missed-payment notice | PASS |
| U9 | CUS-001 | Pay Loan 2 instalments | Confirmation sheet "VND 4,000,000 paid for instalment 1, 2"; progress and history updated | PASS |
| U10 | CUS-001 | Notifications | Repayment received, Instalment overdue, Payment completed, New loan, Pay Later approved | PASS |
| U11 | CUS-002 | Poor profile | "Supporting documents needed" with reasons; payslip + bank statement required; sample uploads → "ready to submit" → submit → "Application under review" (not auto-approved) | PASS |
| U12 | CUS-005 | Thin file | Partner-check card → contextual consent sheet (why / what / if you say no) → Don't allow → partner option removed; documents or manual review offered | PASS |
| U13 | Officer | CUS-002 case | Credit profile & risk indicators (RF-02, RF-03 flagged; thresholds and basis tags), repayment history, documents table, decision panel. **Bug found:** page overflowed horizontally. **Fixed** (grid children allowed to shrink) | FIXED → PASS |
| U14 | Officer | Request documents (bank statement) | Case moves to "Waiting for the customer"; decision panel read-only | PASS |
| U15 | CUS-001 | Scan Mekong 30M | Pay Later disabled with shortfall explanation. **UX issue:** the limit-increase link looked disabled. **Changed** to a prominent "Need more Pay Later credit?" block | FIXED → PASS |
| U16 | CUS-001 | Limit request | Suggested limit = outstanding 8M + purchase 30M = 38M; payslip sample upload; submitted → "Under review" | PASS |
| U17 | Officer | Limit increases tab | Request detail with documents and profile; approved VND 30M (less than requested) → customer limit 30M, available 22M | PASS |
| U18 | CUS-001 | Checkout timer | "Demo: expire now" → "This checkout expired", restart via scan | PASS |

The automated equivalents of these flows are in `tests/` (unit/service/API) and `scripts/e2e_scenarios.py` (persona walkthrough logs in this folder).
