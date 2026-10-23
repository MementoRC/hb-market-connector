"""IbTradingRules: exact-Decimal trading rules derived from IB ContractDetails.

IB reports tick/size constraints as floats, with ``sys.float_info.max``
(UNSET_DOUBLE) or 0 meaning "not provided". Floats are converted through
``repr`` so ``0.0001`` becomes ``Decimal("0.0001")`` rather than its binary
expansion.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

_UNSET_DOUBLE = sys.float_info.max


def _to_decimal(value: float | None) -> Decimal | None:
    """Convert an IB float to an exact Decimal; None when unset (None/0/NaN/UNSET_DOUBLE)."""
    if value is None or value == 0 or value == _UNSET_DOUBLE or math.isnan(value):
        return None
    return Decimal(repr(float(value)))


@dataclass(frozen=True)
class IbTradingRules:
    """Per-contract tick size, size increment, minimum size and supported order types."""

    con_id: int
    min_tick: Decimal
    size_increment: Decimal
    min_size: Decimal
    supports_market_orders: bool
    supports_limit_orders: bool
    order_types: frozenset[str]

    @classmethod
    def from_contract_details(cls, details: Any) -> IbTradingRules:
        """Build rules from an ib_async ContractDetails.

        Unset sizeIncrement defaults to 1; unset minSize falls back to the size
        increment. Unset minTick raises ValueError (naming the conId), since no
        sensible price grid can be assumed.
        """
        con_id = int(details.contract.conId)
        min_tick = _to_decimal(getattr(details, "minTick", None))
        if min_tick is None:
            raise ValueError(f"Contract {con_id} has no minTick in its contract details")
        size_increment = _to_decimal(getattr(details, "sizeIncrement", None)) or Decimal("1")
        min_size = _to_decimal(getattr(details, "minSize", None)) or size_increment
        order_types = frozenset(
            t.strip() for t in (getattr(details, "orderTypes", "") or "").split(",") if t.strip()
        )
        return cls(
            con_id=con_id,
            min_tick=min_tick,
            size_increment=size_increment,
            min_size=min_size,
            supports_market_orders="MKT" in order_types,
            supports_limit_orders="LMT" in order_types,
            order_types=order_types,
        )
