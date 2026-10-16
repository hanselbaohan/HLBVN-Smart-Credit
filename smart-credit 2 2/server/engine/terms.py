"""Repayment schedule from the configured (fictional) demo terms."""
import calendar
from datetime import date
from typing import Any, Dict


def _add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def build_terms(principal_vnd: int, tenure_months: int, start: date, policy: Dict[str, Any]) -> Dict[str, Any]:
    cfg = policy["repayment_terms"]
    if tenure_months not in cfg["offered_tenures"]:
        raise ValueError("Tenure %s is not offered" % tenure_months)
    rate = cfg["annual_interest_rate_pct"]
    fee = cfg["upfront_fee_vnd"]
    rounding = cfg["rounding_vnd"]
    # Flat interest on principal over the tenure (0 in the demo config).
    total_interest = int(round(principal_vnd * rate / 100.0 * tenure_months / 12.0))
    total = principal_vnd + total_interest + fee
    base = (total // tenure_months) // rounding * rounding
    instalments = []
    paid = 0
    for i in range(1, tenure_months + 1):
        amount = base if i < tenure_months else total - paid  # remainder on the last instalment
        paid += amount
        instalments.append({
            "number": i,
            "due_date": _add_months(start, cfg["first_due_after_months"] + i - 1).isoformat(),
            "amount_vnd": amount,
        })
    return {
        "label": cfg["label"],
        "source": cfg["source"],
        "principal_vnd": principal_vnd,
        "tenure_months": tenure_months,
        "annual_interest_rate_pct": rate,
        "total_interest_vnd": total_interest,
        "upfront_fee_vnd": fee,
        "total_repayable_vnd": total,
        "monthly_instalment_vnd": base,
        "instalments": instalments,
        "first_due_date": instalments[0]["due_date"],
        "last_due_date": instalments[-1]["due_date"],
    }
