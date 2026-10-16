"""Credit-decision and payment-transaction state machines.

The two are deliberately independent: a credit APPROVED application can sit
next to a PAYMENT_FAILED transaction.
"""
from typing import Dict, Set


class InvalidTransition(Exception):
    def __init__(self, machine: str, current: str, target: str):
        super().__init__("%s: %s -> %s is not allowed" % (machine, current, target))
        self.machine, self.current, self.target = machine, current, target


CREDIT_TRANSITIONS: Dict[str, Set[str]] = {
    "ASSESSMENT_PENDING": {"STP_ELIGIBLE", "MANUAL_REVIEW", "POLICY_STOP", "MORE_INFORMATION_REQUIRED"},
    "STP_ELIGIBLE": {"APPROVED", "MANUAL_REVIEW"},
    "MANUAL_REVIEW": {"APPROVED", "REJECTED", "MORE_INFORMATION_REQUIRED", "POLICY_STOP"},
    "MORE_INFORMATION_REQUIRED": {"ASSESSMENT_PENDING", "MANUAL_REVIEW", "POLICY_STOP"},
    "APPROVED": set(),
    "REJECTED": set(),
    "POLICY_STOP": set(),
}

TX_TRANSITIONS: Dict[str, Set[str]] = {
    "QR_CREATED": {"PAYMENT_METHOD_SELECTED", "CANCELLED", "EXPIRED"},
    # Pay Later: T&C acceptance is the recorded basis for internal data, so the
    # transaction moves straight to CONSENT_COMPLETED (no separate consent step).
    "PAYMENT_METHOD_SELECTED": {"PAYMENT_METHOD_SELECTED", "CONSENT_PENDING", "CONSENT_COMPLETED",
                                "DISBURSEMENT_PROCESSING", "CANCELLED", "EXPIRED"},
    "CONSENT_PENDING": {"CONSENT_COMPLETED", "PAYMENT_METHOD_SELECTED", "CANCELLED", "EXPIRED"},
    "CONSENT_COMPLETED": {"ASSESSMENT_IN_PROGRESS", "CANCELLED", "EXPIRED"},
    "ASSESSMENT_IN_PROGRESS": {"PENDING_REVIEW", "TERMS_PENDING_CONFIRMATION", "CANCELLED"},
    "PENDING_REVIEW": {"ASSESSMENT_IN_PROGRESS", "TERMS_PENDING_CONFIRMATION", "CANCELLED"},
    "TERMS_PENDING_CONFIRMATION": {"DISBURSEMENT_PROCESSING", "CANCELLED", "EXPIRED"},
    "DISBURSEMENT_PROCESSING": {"PAYMENT_COMPLETED", "PAYMENT_FAILED"},
    "PAYMENT_FAILED": {"DISBURSEMENT_PROCESSING", "CANCELLED", "EXPIRED"},
    "PAYMENT_COMPLETED": set(),
    "CANCELLED": set(),
    "EXPIRED": set(),
}

CREDIT_TERMINAL = {"APPROVED", "REJECTED", "POLICY_STOP"}
TX_TERMINAL = {"PAYMENT_COMPLETED", "CANCELLED", "EXPIRED"}
# Customer-side checkout steps covered by the 5-minute timer. PENDING_REVIEW
# (officer review or document gathering) never expires.
TX_EXPIRABLE = {s for s, t in TX_TRANSITIONS.items() if "EXPIRED" in t}

LIMIT_TRANSITIONS: Dict[str, Set[str]] = {
    "DRAFT": {"PENDING_REVIEW", "CANCELLED"},
    "PENDING_REVIEW": {"APPROVED", "REJECTED", "MORE_INFORMATION_REQUIRED", "CANCELLED"},
    "MORE_INFORMATION_REQUIRED": {"PENDING_REVIEW", "CANCELLED"},
    "APPROVED": set(), "REJECTED": set(), "CANCELLED": set(),
}
TX_ACTIVE_BEFORE_PAYMENT = set(TX_TRANSITIONS) - TX_TERMINAL - {"DISBURSEMENT_PROCESSING"}


def check_credit(current: str, target: str) -> None:
    if target not in CREDIT_TRANSITIONS.get(current, set()):
        raise InvalidTransition("credit", current, target)


def check_tx(current: str, target: str) -> None:
    if target not in TX_TRANSITIONS.get(current, set()):
        raise InvalidTransition("transaction", current, target)


def check_limit(current: str, target: str) -> None:
    if target not in LIMIT_TRANSITIONS.get(current, set()):
        raise InvalidTransition("limit request", current, target)
