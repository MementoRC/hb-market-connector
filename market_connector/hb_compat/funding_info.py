"""Legacy hummingbot ``FundingInfo`` / ``FundingInfoUpdate``.

Mirrors ``hummingbot.core.data_type.funding_info``. Drop-in shape for re-export
shims: positional constructor, read-only ``trading_pair``, mutable setters, and
``update()``. ``to_primitive()`` / ``from_primitive()`` convert to and from the
frozen canonical ``market_connector.primitives.FundingInfo``.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Self

from market_connector.primitives import FundingInfo as FundingInfoPrimitive


@dataclass
class FundingInfoUpdate:
    """Partial update for a ``FundingInfo``; ``None`` fields are left unchanged."""

    trading_pair: str
    index_price: Decimal | None = None
    mark_price: Decimal | None = None
    next_funding_utc_timestamp: int | None = None
    rate: Decimal | None = None


class FundingInfo:
    """Mutable funding snapshot of a perpetual market (hummingbot ``FundingInfo``)."""

    def __init__(
        self,
        trading_pair: str,
        index_price: Decimal,
        mark_price: Decimal,
        next_funding_utc_timestamp: int,
        rate: Decimal,
    ) -> None:
        self._trading_pair = trading_pair
        self._index_price = index_price
        self._mark_price = mark_price
        self._next_funding_utc_timestamp = next_funding_utc_timestamp
        self._rate = rate

    @property
    def trading_pair(self) -> str:
        return self._trading_pair

    @property
    def index_price(self) -> Decimal:
        return self._index_price

    @index_price.setter
    def index_price(self, index_price: Decimal) -> None:
        self._index_price = index_price

    @property
    def mark_price(self) -> Decimal:
        return self._mark_price

    @mark_price.setter
    def mark_price(self, mark_price: Decimal) -> None:
        self._mark_price = mark_price

    @property
    def next_funding_utc_timestamp(self) -> int:
        return self._next_funding_utc_timestamp

    @next_funding_utc_timestamp.setter
    def next_funding_utc_timestamp(self, next_funding_utc_timestamp: int) -> None:
        self._next_funding_utc_timestamp = next_funding_utc_timestamp

    @property
    def rate(self) -> Decimal:
        return self._rate

    @rate.setter
    def rate(self, rate: Decimal) -> None:
        self._rate = rate

    def update(self, info_update: FundingInfoUpdate) -> None:
        """Apply the non-None fields of ``info_update``; ``trading_pair`` is never changed."""
        if info_update.index_price is not None:
            self._index_price = info_update.index_price
        if info_update.mark_price is not None:
            self._mark_price = info_update.mark_price
        if info_update.next_funding_utc_timestamp is not None:
            self._next_funding_utc_timestamp = info_update.next_funding_utc_timestamp
        if info_update.rate is not None:
            self._rate = info_update.rate

    def to_primitive(self) -> FundingInfoPrimitive:
        """Convert to the frozen canonical ``FundingInfo``."""
        return FundingInfoPrimitive(
            trading_pair=self._trading_pair,
            index_price=self._index_price,
            mark_price=self._mark_price,
            next_funding_utc_timestamp=self._next_funding_utc_timestamp,
            rate=self._rate,
        )

    @classmethod
    def from_primitive(cls, primitive: FundingInfoPrimitive) -> Self:
        """Build a legacy ``FundingInfo`` from the frozen canonical model."""
        return cls(
            primitive.trading_pair,
            primitive.index_price,
            primitive.mark_price,
            primitive.next_funding_utc_timestamp,
            primitive.rate,
        )
