"""IbWhatIfResult: exact-Decimal margin and commission preview from an IB what-if OrderState.

IB reports margin figures as strings and commissions as floats. Empty strings,
``sys.float_info.max`` (UNSET_DOUBLE) and NaN mean "not provided" and map to
None. Zero is a real value here (a free trade, an unchanged margin), unlike in
trading rules. Floats are converted through ``repr`` so ``0.35`` becomes
``Decimal("0.35")`` rather than its binary expansion.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

_UNSET_DOUBLE = sys.float_info.max
# IB serialises the sentinel via repr, which is just below the exact binary value.
_UNSET_MARGIN = Decimal(repr(_UNSET_DOUBLE))


def _margin(value: Any) -> Decimal | None:
    """Parse an IB margin string; None when empty, unparsable, non-finite or the unset sentinel."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = Decimal(value.strip())
    except InvalidOperation:
        return None
    if not parsed.is_finite() or abs(parsed) >= _UNSET_MARGIN:
        return None
    return parsed


def _commission(value: Any) -> Decimal | None:
    """Convert an IB commission float; None when None, NaN or the unset sentinel."""
    if value is None or value == _UNSET_DOUBLE or math.isnan(value):
        return None
    return Decimal(repr(float(value)))


def _text(value: Any) -> str | None:
    """Return a non-empty string, else None."""
    return value if isinstance(value, str) and value else None


@dataclass(frozen=True)
class IbWhatIfResult:
    """Projected margin, equity-with-loan and commission impact of a not-yet-placed order."""

    init_margin_before: Decimal | None
    init_margin_change: Decimal | None
    init_margin_after: Decimal | None
    maint_margin_before: Decimal | None
    maint_margin_change: Decimal | None
    maint_margin_after: Decimal | None
    equity_with_loan_before: Decimal | None
    equity_with_loan_change: Decimal | None
    equity_with_loan_after: Decimal | None
    commission: Decimal | None
    min_commission: Decimal | None
    max_commission: Decimal | None
    commission_currency: str | None
    warning_text: str | None

    @classmethod
    def from_order_state(cls, state: Any) -> IbWhatIfResult:
        """Build a result from an ib_async OrderState returned by a what-if order."""
        return cls(
            init_margin_before=_margin(getattr(state, "initMarginBefore", None)),
            init_margin_change=_margin(getattr(state, "initMarginChange", None)),
            init_margin_after=_margin(getattr(state, "initMarginAfter", None)),
            maint_margin_before=_margin(getattr(state, "maintMarginBefore", None)),
            maint_margin_change=_margin(getattr(state, "maintMarginChange", None)),
            maint_margin_after=_margin(getattr(state, "maintMarginAfter", None)),
            equity_with_loan_before=_margin(getattr(state, "equityWithLoanBefore", None)),
            equity_with_loan_change=_margin(getattr(state, "equityWithLoanChange", None)),
            equity_with_loan_after=_margin(getattr(state, "equityWithLoanAfter", None)),
            commission=_commission(getattr(state, "commission", None)),
            min_commission=_commission(getattr(state, "minCommission", None)),
            max_commission=_commission(getattr(state, "maxCommission", None)),
            commission_currency=_text(getattr(state, "commissionCurrency", None)),
            warning_text=_text(getattr(state, "warningText", None)),
        )
