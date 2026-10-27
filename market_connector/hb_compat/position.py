"""Legacy hummingbot ``Position`` (hummingbot.connector.derivative.position).

Drop-in shape for re-export shims: positional constructor, read-only properties,
``update_position()`` and hummingbot's exact ``__repr__``. ``position_side`` may be
the canonical ``PositionSide``, any enum-like with a ``.value``, or a plain string;
``to_primitive()`` normalises it to the canonical ``PositionSide``.
"""

from decimal import Decimal
from enum import Enum
from typing import Self

from data_type_primitives import PositionSide

from market_connector.primitives import Position as PositionPrimitive

type PositionSideLike = PositionSide | Enum | str


class Position:
    """An open derivative position (hummingbot ``Position``)."""

    def __init__(
        self,
        trading_pair: str,
        position_side: PositionSideLike,
        unrealized_pnl: Decimal,
        entry_price: Decimal,
        amount: Decimal,
        leverage: Decimal,
    ) -> None:
        self._trading_pair = trading_pair
        self._position_side = position_side
        self._unrealized_pnl = unrealized_pnl
        self._entry_price = entry_price
        self._amount = amount
        self._leverage = leverage

    @property
    def trading_pair(self) -> str:
        return self._trading_pair

    @property
    def position_side(self) -> PositionSideLike:
        return self._position_side

    @property
    def unrealized_pnl(self) -> Decimal:
        return self._unrealized_pnl

    @property
    def entry_price(self) -> Decimal:
        return self._entry_price

    @property
    def amount(self) -> Decimal:
        return self._amount

    @property
    def leverage(self) -> Decimal:
        return self._leverage

    def update_position(
        self,
        position_side: PositionSideLike | None = None,
        unrealized_pnl: Decimal | None = None,
        entry_price: Decimal | None = None,
        amount: Decimal | None = None,
        leverage: Decimal | None = None,
    ) -> None:
        """Replace the given fields; ``None`` arguments leave their field unchanged."""
        if position_side is not None:
            self._position_side = position_side
        if unrealized_pnl is not None:
            self._unrealized_pnl = unrealized_pnl
        if entry_price is not None:
            self._entry_price = entry_price
        if amount is not None:
            self._amount = amount
        if leverage is not None:
            self._leverage = leverage

    def __repr__(self) -> str:
        return (
            f"Position( trading_pair={self._trading_pair}, position_side={self._position_side}, "
            f"unrealized_pnl={self._unrealized_pnl}, entry_price={self._entry_price}, "
            f"amount={self._amount}, leverage={self._leverage})"
        )

    def to_primitive(self) -> PositionPrimitive:
        """Convert to the frozen canonical ``Position``, mapping the side by ``.value``."""
        raw: object = (
            self._position_side.value
            if isinstance(self._position_side, Enum)
            else self._position_side
        )
        return PositionPrimitive(
            trading_pair=self._trading_pair,
            position_side=PositionSide(raw),
            unrealized_pnl=self._unrealized_pnl,
            entry_price=self._entry_price,
            amount=self._amount,
            leverage=self._leverage,
        )

    @classmethod
    def from_primitive(cls, primitive: PositionPrimitive) -> Self:
        """Build a legacy ``Position`` from the frozen canonical model."""
        return cls(
            primitive.trading_pair,
            primitive.position_side,
            primitive.unrealized_pnl,
            primitive.entry_price,
            primitive.amount,
            primitive.leverage,
        )
