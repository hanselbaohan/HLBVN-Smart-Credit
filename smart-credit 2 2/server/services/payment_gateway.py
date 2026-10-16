"""SIMULATED payment / disbursement gateway. No real payment rail is called.

Outcome is deterministic so judges can reproduce it:
  * a QR fixture can declare ``simulated_failures`` = N -> the first N attempts fail;
  * the demo control ``force_next_payment_failure`` fails exactly one attempt;
  * account payments fail when the fictional balance is insufficient.
"""
from typing import Any, Dict


def process(method: str, amount_vnd: int, attempt_no: int, qr: Dict[str, Any], balance_vnd: int,
            force_failure: bool) -> Dict[str, Any]:
    if force_failure:
        return {"ok": False, "code": "SIMULATED_FORCED_FAILURE",
                "message": "Simulated processing failure (demo control)."}
    if attempt_no <= int(qr.get("simulated_failures") or 0):
        return {"ok": False, "code": "SIMULATED_ACQUIRER_TIMEOUT",
                "message": "The simulated merchant acquirer did not respond in time."}
    if method == "ACCOUNT" and balance_vnd < amount_vnd:
        return {"ok": False, "code": "INSUFFICIENT_FUNDS",
                "message": "Your fictional account balance is not enough for this purchase."}
    return {"ok": True, "code": "SIMULATED_SUCCESS", "message": "Simulated payment completed."}
