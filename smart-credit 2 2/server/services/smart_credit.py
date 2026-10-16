"""Smart Credit application service.

Composed from focused mixins; all state changes are validated against
server/domain/states.py, run inside a single SQLite transaction, and write
append-only audit events.
"""
from .base import ACCOUNT, PAY_LATER, BaseService, ServiceError  # noqa: F401  (re-exported)
from .checkout import CheckoutMixin
from .limits import LimitMixin
from .officer import OfficerMixin
from .servicing import ServicingMixin


class SmartCreditService(CheckoutMixin, OfficerMixin, ServicingMixin, LimitMixin, BaseService):
    pass
