# Peer / customer testing kit (not yet run with real people)

The prototype has been validated with **mock personas** (automated logs in `docs/test_logs/`) and a scripted UI walkthrough by the build agent. It has **not** been tested with peers or target borrowers yet. Use this kit to run and record those sessions so the results are verifiable.

## Setup (per session, ~20 min)
1. `python3 run.py --reset` on the facilitator laptop and open `http://127.0.0.1:8080` in a phone-sized browser window. Open `/officer.html` (code `DEMO-OFFICER-2026`) on a second screen for the facilitator.
2. Tell participants that everything is fictional and simulated, and that they should never enter real banking credentials.
3. Record: participant ID (P01…), segment (peer / retail borrower / gig worker / first-time borrower), device, date.

## Tasks (read aloud, don't help unless stuck > 60 s)
| Task | Persona to select | Success = | Observe |
|---|---|---|---|
| T1 Buy the smartphone with Pay Later | Ta My Kim Tien → QR ABC Electronics 12M | Reaches "Payment completed" | Did they read/understand the single T&C box? Did they notice "approved ≠ paid"? |
| T2 Find when your first instalment is due and pay it | same | Pays instalment 1 in My Loans | Can they find Pay Loan from Home? |
| T3 Buy the laptop accessories | Huynh Gia Bao → QR ABC 6.4M | Uploads 2 documents and understands the case is under review | Do they feel rejected? Is the "why" clear? |
| T4 Buy the course | Dang Thu Ha → QR Saigon Skills 7.2M | Makes an informed choice on the partner-data pop-up | Do they understand what is shared and that "no" is OK? |
| T5 Buy the electric scooter | Ta My Kim Tien → QR Mekong 30M | Finds and submits a limit-increase request | Is the alternative to rejection clear? |
| T6 Leave a purchase unfinished for 5 minutes (or use "Demo: expire now") | any | Understands why it expired and how to restart | Countdown visibility |

## Record per task
| Participant | Task | Completed (Y/N) | Time (s) | Errors / wrong taps | Confidence 1–5 | Quote | Facilitator note |
|---|---|---|---|---|---|---|---|

## Post-session questions (1–5 Likert + why)
1. I understood when my purchase was approved versus actually paid.
2. I understood why extra documents were requested.
3. I felt in control of what data was shared.
4. I would use QR Pay Later for a purchase like this.
5. The app looked and felt like HLB Connect.

## Evidence to keep
- The filled tables above (anonymised), plus the server's audit export: `curl -H "X-Demo-Officer-Code: DEMO-OFFICER-2026" http://127.0.0.1:8080/api/officer/audit > session_P01_audit.json`.
- Don't record real names, phone numbers or account details.
