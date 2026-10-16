"""Loan servicing: My Loans, Pay Loan repayments, reminders, overdue handling,
notifications and the simulated business date. Nothing is reported to CIC."""
import json
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from ..store.db import now_iso
from .base import ServiceError, _j


class ServicingMixin:
    # ------------------------------------------------------------ credit position
    def credit_summary(self, customer_id: str) -> Dict[str, Any]:
        c = self._customer(customer_id)
        outstanding = self.db.one(
            "SELECT COALESCE(SUM(i.amount_vnd), 0) AS n FROM loan_instalments i JOIN loans l USING(loan_ref) "
            "WHERE l.customer_id = ? AND i.status != 'PAID'", (customer_id,))["n"]
        open_req = self.db.one("SELECT request_id, status FROM limit_requests WHERE customer_id = ? "
                               "AND status IN ('DRAFT', 'PENDING_REVIEW', 'MORE_INFORMATION_REQUIRED') ORDER BY rowid DESC LIMIT 1",
                               (customer_id,))
        limit = c["indicative_limit_vnd"] or 0
        return {"limit_vnd": limit, "outstanding_vnd": outstanding, "available_vnd": max(0, limit - outstanding),
                "internal_delinquency_flag": c["internal_delinquency_flag"], "open_limit_request": open_req,
                "label": "Fictional indicative limit - not an approval or available funds"}

    # ------------------------------------------------------------ business date (demo control)
    def set_business_date(self, actor: str, new_date: Optional[str] = None, advance_days: Optional[int] = None) -> Dict[str, Any]:
        cur = self.as_of
        if new_date:
            try:
                target = datetime.strptime(new_date, "%Y-%m-%d").date()
            except ValueError:
                raise ServiceError(422, "INVALID_DATE", "Use YYYY-MM-DD.")
        elif isinstance(advance_days, int) and not isinstance(advance_days, bool):
            target = cur + timedelta(days=advance_days)
        else:
            raise ServiceError(422, "INVALID_CLOCK", "Give a date or advance_days.")
        if target < self.demo_date:
            raise ServiceError(422, "INVALID_DATE", "The demo date can't go before %s." % self.demo_date.isoformat())
        with self._unit():
            self.db.set_control("business_date", target.isoformat())
            self.db.audit("DEMO_CONTROL", actor, "BUSINESS_DATE_SET", details={"from": cur.isoformat(), "to": target.isoformat()})
        self.refresh_servicing()
        return {"business_date": target.isoformat()}

    # ------------------------------------------------------------ loans
    def _book_loan(self, app: Dict[str, Any], tx: Dict[str, Any], merchant_name: str) -> str:
        terms = _j(app["terms_json"])
        loan_ref = self._new_id("LN", "loan")
        self.db.run("INSERT INTO loans(loan_ref, application_id, transaction_id, customer_id, principal_vnd, terms_json, "
                    "created_at, merchant_name, status) VALUES (?,?,?,?,?,?,?,?,?)",
                    (loan_ref, app["application_id"], tx["transaction_id"], tx["customer_id"], tx["amount_vnd"],
                     app["terms_json"], now_iso(), merchant_name, "ACTIVE"))
        for i in terms["instalments"]:
            self.db.run("INSERT INTO loan_instalments(loan_ref, number, due_date, amount_vnd) VALUES (?,?,?,?)",
                        (loan_ref, i["number"], i["due_date"], i["amount_vnd"]))
        self.db.audit("SYSTEM", "simulator", "SIMULATED_LOAN_BOOKED", tx["transaction_id"], app["application_id"],
                      details={"loan_ref": loan_ref, "instalments": len(terms["instalments"]),
                               "first_due": terms["first_due_date"]})
        self.notify(tx["customer_id"], "LOAN", "New loan in My Loans",
                    "%s: %d instalments, first due %s." % (merchant_name, len(terms["instalments"]), terms["first_due_date"]),
                    "#/loans/" + loan_ref, "loan:" + loan_ref)
        return loan_ref

    def refresh_servicing(self, customer_id: Optional[str] = None) -> None:
        """Apply demo reminders, overdue fees, internal delinquency and simulated default status."""
        cfg = self.policy["loan_servicing"]
        today = self.as_of
        sql = ("SELECT i.*, l.customer_id, l.merchant_name FROM loan_instalments i JOIN loans l USING(loan_ref) "
               "WHERE i.status != 'PAID'")
        args: List[Any] = []
        if customer_id:
            sql += " AND l.customer_id = ?"
            args.append(customer_id)
        for i in self.db.all(sql, args):
            due = datetime.strptime(i["due_date"], "%Y-%m-%d").date()
            link = "#/loans/" + i["loan_ref"]
            key = "%s:%s" % (i["loan_ref"], i["number"])
            amount = "{:,}".format(i["amount_vnd"])
            with self._unit():
                if due - timedelta(days=cfg["reminder_days_before_due"]) <= today < due:
                    self.notify(i["customer_id"], "REMINDER", "Instalment due in %d day(s)" % (due - today).days,
                                "%s instalment %d: VND %s due %s." % (i["merchant_name"], i["number"], amount, i["due_date"]),
                                link, "remind:" + key)
                elif today == due:
                    self.notify(i["customer_id"], "REMINDER", "Instalment due today",
                                "%s instalment %d: VND %s is due today." % (i["merchant_name"], i["number"], amount),
                                link, "due:" + key)
                elif today > due and i["status"] != "OVERDUE":
                    fee = int(cfg["late_fee_vnd"] or 0)
                    self.db.run("UPDATE loan_instalments SET status = 'OVERDUE', late_fee_vnd = ? WHERE loan_ref = ? AND number = ?",
                                (fee, i["loan_ref"], i["number"]))
                    if cfg["missed_payment_creates_internal_flag"]:
                        self.db.run("UPDATE customers SET internal_delinquency_flag = 1 WHERE customer_id = ?", (i["customer_id"],))
                    self.db.audit("SYSTEM", "servicing", "INSTALMENT_OVERDUE",
                                  details={"loan_ref": i["loan_ref"], "number": i["number"], "due_date": i["due_date"],
                                           "late_fee_vnd": fee, "internal_flag": "RF-01 set (simulated, not reported to CIC)"})
                    self.notify(i["customer_id"], "OVERDUE", "Instalment overdue",
                                "%s instalment %d (VND %s) was due %s. %s" % (
                                    i["merchant_name"], i["number"], amount, i["due_date"],
                                    "Late fee: VND {:,}.".format(fee) if fee else "No late fee is configured in this demo."),
                                link, "overdue:" + key)

        # Demo-only default: a loan becomes DEFAULT when any unpaid instalment is
        # more than the configured number of days overdue. Never reported to CIC.
        default_days = int(cfg.get("default_after_days_overdue", 90))
        if default_days > 0:
            rows = self.db.all(
                "SELECT DISTINCT l.loan_ref, l.customer_id, i.due_date FROM loans l "
                "JOIN loan_instalments i USING(loan_ref) "
                "WHERE l.status = 'ACTIVE' AND i.status != 'PAID'")
            seen = set()
            for row in rows:
                overdue_days = (today - datetime.strptime(row["due_date"], "%Y-%m-%d").date()).days
                if overdue_days > default_days and row["loan_ref"] not in seen:
                    seen.add(row["loan_ref"])
                    with self._unit():
                        self.db.run("UPDATE loans SET status = 'DEFAULT' WHERE loan_ref = ? AND status = 'ACTIVE'",
                                    (row["loan_ref"],))
                        self.db.run("UPDATE customers SET internal_delinquency_flag = 1 WHERE customer_id = ?",
                                    (row["customer_id"],))
                        self.db.audit("SYSTEM", "servicing", "SIMULATED_DEFAULT",
                                      details={"loan_ref": row["loan_ref"], "days_overdue": overdue_days,
                                               "threshold_days": default_days,
                                               "note": "Demo-only internal status; not reported to CIC."})
                        self.notify(row["customer_id"], "OVERDUE", "Loan marked as demo default",
                                    "%s has an instalment more than %d days overdue. This is a simulated internal demo status only; nothing is reported to CIC." %
                                    (row["loan_ref"], default_days), "#/loans/" + row["loan_ref"],
                                    "default:" + row["loan_ref"])

    def _loan_view(self, loan: Dict[str, Any]) -> Dict[str, Any]:
        inst = self.db.all("SELECT * FROM loan_instalments WHERE loan_ref = ? ORDER BY number", (loan["loan_ref"],))
        today = self.as_of
        for i in inst:
            i["total_due_vnd"] = i["amount_vnd"] + i["late_fee_vnd"]
            if i["status"] == "SCHEDULED":
                due = datetime.strptime(i["due_date"], "%Y-%m-%d").date()
                i["display_status"] = "DUE_TODAY" if due == today else ("DUE_SOON" if 0 < (due - today).days <=
                                                                         self.policy["loan_servicing"]["reminder_days_before_due"]
                                                                         else "SCHEDULED")
            else:
                i["display_status"] = i["status"]
        paid = [i for i in inst if i["status"] == "PAID"]
        unpaid = [i for i in inst if i["status"] != "PAID"]
        reps = self.db.all("SELECT * FROM loan_repayments WHERE loan_ref = ? ORDER BY created_at DESC, rowid DESC", (loan["loan_ref"],))
        for r in reps:
            r["instalments"] = _j(r.pop("instalments_json"), [])
            r.pop("idempotency_key", None)
        terms = _j(loan["terms_json"], {})
        return {
            "loan_ref": loan["loan_ref"], "merchant_name": loan["merchant_name"], "status": loan["status"],
            "transaction_id": loan["transaction_id"], "application_id": loan["application_id"],
            "principal_vnd": loan["principal_vnd"], "created_at": loan["created_at"], "closed_at": loan["closed_at"],
            "terms_label": terms.get("label"), "tenure_months": terms.get("tenure_months"),
            "outstanding_vnd": sum(i["total_due_vnd"] for i in unpaid),
            "paid_count": len(paid), "instalment_count": len(inst),
            "overdue_count": sum(1 for i in unpaid if i["status"] == "OVERDUE"),
            "next_due": unpaid[0] if unpaid else None, "instalments": inst, "repayments": reps,
            "business_date": today.isoformat(),
        }

    def list_loans(self, customer_id: str) -> Dict[str, Any]:
        self._customer(customer_id)
        self.refresh_servicing(customer_id)
        loans = [self._loan_view(l) for l in self.db.all("SELECT * FROM loans WHERE customer_id = ? ORDER BY created_at DESC, rowid DESC",
                                                          (customer_id,))]
        return {"business_date": self.as_of.isoformat(), "loans": loans, "credit": self.credit_summary(customer_id)}

    def get_loan(self, loan_ref: str, customer_id: str) -> Dict[str, Any]:
        self.refresh_servicing(customer_id)
        loan = self.db.one("SELECT * FROM loans WHERE loan_ref = ?", (loan_ref,))
        if not loan:
            raise ServiceError(404, "LOAN_NOT_FOUND", "Loan %s not found." % loan_ref)
        if loan["customer_id"] != customer_id:
            raise ServiceError(403, "FORBIDDEN", "This loan belongs to another demo customer.")
        out = self._loan_view(loan)
        out["account"] = {k: self._customer(customer_id)[k] for k in ("account_masked", "balance_vnd")}
        return out

    def repay(self, loan_ref: str, customer_id: str, numbers: List[int], idempotency_key: str) -> Dict[str, Any]:
        """Pay Loan: simulated debit from the customer's account for the selected instalments
        (oldest unpaid first)."""
        if not idempotency_key or len(idempotency_key) > 100:
            raise ServiceError(422, "IDEMPOTENCY_KEY_REQUIRED", "An Idempotency-Key header is required.")
        if not numbers or not all(isinstance(n, int) and not isinstance(n, bool) for n in numbers):
            raise ServiceError(422, "SELECT_INSTALMENTS", "Select at least one instalment to pay.")
        self.refresh_servicing(customer_id)
        prior = self.db.one("SELECT * FROM loan_repayments WHERE idempotency_key = ?", (idempotency_key,))
        if prior:
            if prior["loan_ref"] != loan_ref:
                raise ServiceError(409, "IDEMPOTENCY_KEY_REUSED", "This key was used for another repayment.")
            out = self.get_loan(loan_ref, customer_id)
            out["idempotent_replay"] = True
            return out
        with self._unit():
            loan = self.db.one("SELECT * FROM loans WHERE loan_ref = ?", (loan_ref,))
            if not loan or loan["customer_id"] != customer_id:
                raise ServiceError(404, "LOAN_NOT_FOUND", "Loan %s not found." % loan_ref)
            if loan["status"] not in ("ACTIVE", "DEFAULT"):
                raise ServiceError(409, "LOAN_CLOSED", "This loan is already fully repaid.")
            unpaid = self.db.all("SELECT * FROM loan_instalments WHERE loan_ref = ? AND status != 'PAID' ORDER BY number",
                                 (loan_ref,))
            wanted = sorted(set(numbers))
            expected = [i["number"] for i in unpaid[:len(wanted)]]
            if wanted != expected:
                raise ServiceError(422, "PAY_OLDEST_FIRST", "Please pay the oldest unpaid instalments first (instalment %s)."
                                   % (unpaid[0]["number"] if unpaid else "-"), {"next_unpaid": expected})
            amount = sum(i["amount_vnd"] + i["late_fee_vnd"] for i in unpaid[:len(wanted)])
            cust = self._customer(customer_id)
            rep_id = self._new_id("RP", "repayment")
            if cust["balance_vnd"] < amount:
                self.db.run("INSERT INTO loan_repayments VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (rep_id, loan_ref, customer_id, json.dumps(wanted), amount, idempotency_key, "FAILED",
                             "Insufficient fictional account balance.", self.as_of.isoformat(), now_iso()))
                self.db.audit("CUSTOMER", customer_id, "LOAN_REPAYMENT_FAILED", loan["transaction_id"], loan["application_id"],
                              details={"loan_ref": loan_ref, "repayment_id": rep_id, "amount_vnd": amount,
                                       "reason": "INSUFFICIENT_FUNDS"})
                failure = ServiceError(409, "INSUFFICIENT_FUNDS", "Your account balance (VND {:,}) is not enough for VND {:,}."
                                       .format(cust["balance_vnd"], amount))
            else:
                failure = None
                self._apply_repayment(loan, customer_id, wanted, amount, rep_id, idempotency_key)
        if failure:  # the failed attempt is committed (recorded) before reporting it
            raise failure
        return self.get_loan(loan_ref, customer_id)

    def _apply_repayment(self, loan: Dict[str, Any], customer_id: str, wanted: List[int], amount: int, rep_id: str,
                         idempotency_key: str) -> None:
            loan_ref = loan["loan_ref"]
            ts = now_iso()
            self.db.run("UPDATE customers SET balance_vnd = balance_vnd - ? WHERE customer_id = ?", (amount, customer_id))
            for n in wanted:
                self.db.run("UPDATE loan_instalments SET status = 'PAID', paid_at = ?, repayment_id = ? WHERE loan_ref = ? AND number = ?",
                            (ts, rep_id, loan_ref, n))
            self.db.run("INSERT INTO loan_repayments VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (rep_id, loan_ref, customer_id, json.dumps(wanted), amount, idempotency_key, "SUCCEEDED", None,
                         self.as_of.isoformat(), ts))
            left = self.db.one("SELECT COUNT(*) n FROM loan_instalments WHERE loan_ref = ? AND status != 'PAID'", (loan_ref,))["n"]
            if left == 0:
                self.db.run("UPDATE loans SET status = 'CLOSED', closed_at = ? WHERE loan_ref = ?", (ts, loan_ref))
            self.db.audit("CUSTOMER", customer_id, "LOAN_REPAYMENT", loan["transaction_id"], loan["application_id"],
                          details={"loan_ref": loan_ref, "repayment_id": rep_id, "instalments": wanted, "amount_vnd": amount,
                                   "remaining_instalments": left, "simulated": "account debit"})
            self.notify(customer_id, "LOAN", "Repayment received", "VND {:,} paid on {} ({} instalment(s)). {}".format(
                amount, loan["merchant_name"], len(wanted), "Loan fully repaid." if left == 0 else "%d instalment(s) left." % left),
                "#/loans/" + loan_ref, "repay:" + rep_id)

    # ------------------------------------------------------------ notifications
    def notifications(self, customer_id: str) -> Dict[str, Any]:
        self._customer(customer_id)
        self.sweep_expired()
        self.refresh_servicing(customer_id)
        rows = self.db.all("SELECT * FROM notifications WHERE customer_id = ? ORDER BY notification_id DESC LIMIT 100",
                           (customer_id,))
        return {"business_date": self.as_of.isoformat(), "items": rows,
                "unread": sum(1 for r in rows if not r["read_at"])}

    def mark_notifications_read(self, customer_id: str) -> Dict[str, Any]:
        with self._unit():
            self.db.run("UPDATE notifications SET read_at = ? WHERE customer_id = ? AND read_at IS NULL", (now_iso(), customer_id))
        return self.notifications(customer_id)
