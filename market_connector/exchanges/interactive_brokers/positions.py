"""IbPosition: one open position of an IB account, with exact-Decimal quantities.

IB reports position size and average cost as floats; they are converted through
``str`` so ``0.5`` becomes ``Decimal("0.5")``. ``quantity`` is negative for shorts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from decimal import Decimal


@dataclass(frozen=True)
class IbPosition:
    """A held position: contract identity plus signed quantity and average cost."""

    account: str
    symbol: str
    sec_type: str
    con_id: int
    exchange: str
    currency: str
    quantity: Decimal
    avg_cost: Decimal
