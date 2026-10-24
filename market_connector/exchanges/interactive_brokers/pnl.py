"""IbPnL / IbPositionPnL: exact-Decimal profit-and-loss snapshots from IB reqPnL / reqPnLSingle.

IB reports PnL figures as floats. NaN (ib_async's default for "not yet received") and
``sys.float_info.max`` (UNSET_DOUBLE) mean "not provided" and map to None. Zero is a real
value. Floats are converted through ``repr`` so ``12.34`` becomes ``Decimal("12.34")``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from market_connector.exchanges.interactive_brokers._ib_numbers import (
    decimal_or_none as _decimal_or_none,
)


@dataclass(frozen=True)
class IbPnL:
    """Account-level daily, unrealized and realized PnL."""

    account: str
    daily_pnl: Decimal | None
    unrealized_pnl: Decimal | None
    realized_pnl: Decimal | None

    @classmethod
    def from_pnl(cls, pnl: Any) -> IbPnL:
        """Build from an ib_async PnL object."""
        return cls(
            account=pnl.account,
            daily_pnl=_decimal_or_none(pnl.dailyPnL),
            unrealized_pnl=_decimal_or_none(pnl.unrealizedPnL),
            realized_pnl=_decimal_or_none(pnl.realizedPnL),
        )


@dataclass(frozen=True)
class IbPositionPnL:
    """Daily, unrealized and realized PnL plus market value of one position."""

    account: str
    con_id: int
    position: Decimal
    daily_pnl: Decimal | None
    unrealized_pnl: Decimal | None
    realized_pnl: Decimal | None
    value: Decimal | None

    @classmethod
    def from_pnl_single(cls, pnl: Any) -> IbPositionPnL:
        """Build from an ib_async PnLSingle object."""
        return cls(
            account=pnl.account,
            con_id=pnl.conId,
            position=Decimal(repr(float(pnl.position))),
            daily_pnl=_decimal_or_none(pnl.dailyPnL),
            unrealized_pnl=_decimal_or_none(pnl.unrealizedPnL),
            realized_pnl=_decimal_or_none(pnl.realizedPnL),
            value=_decimal_or_none(pnl.value),
        )
