"""MarketDataMixin: get_orderbook, get_mid_price, get_candles for IB gateway."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from market_connector.exchanges.interactive_brokers.mixins.protocols import (  # noqa: F401
        HasContractResolver,
        HasIbTransport,
    )
    from market_connector.primitives import CandleData

_INTERVAL_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}


def _duration_string(interval: str, limit: int) -> str:
    """Compute IB durationStr from interval and bar count.

    IB accepts "N S" for up to ~7200 seconds, "N D" for day-based.
    Formula: total_seconds = interval_seconds * limit.
    If total_seconds <= 86400 → "N S". Otherwise → "N D" (ceiling days).
    1d bars always use "N D" directly.
    """
    if interval == "1d":
        return f"{limit} D"
    secs = _INTERVAL_SECONDS.get(interval, 0) * limit
    if secs <= 86400:
        return f"{secs} S"
    days = (secs + 86399) // 86400
    return f"{days} D"


class MarketDataMixin:
    """Mixin providing market-data snapshot methods for IbGatewayGateway.

    Self-type contract: the concrete class must also satisfy HasIbTransport
    and HasContractResolver (verified by mypy at static analysis time).
    """

    async def get_orderbook(
        self,
        trading_pair: str,
    ) -> object:  # returns OrderBookSnapshot; typed loosely to avoid circular import
        """Snapshot DOM depth for trading_pair from the transport's shared depth stream."""
        contract = await self._contract_resolver.resolve_from_pair(trading_pair)  # type: ignore[attr-defined]
        bids, asks = await self._transport.depth_snapshot(contract)  # type: ignore[attr-defined]

        # Late import to avoid circular dependency with framework.
        from market_connector.primitives import OrderBookSnapshot  # noqa: PLC0415

        return OrderBookSnapshot(
            trading_pair=trading_pair,
            bids=bids,
            asks=asks,
            timestamp=time.time(),
        )

    async def get_mid_price(
        self: HasIbTransport & HasContractResolver,  # type: ignore[valid-type]
        trading_pair: str,
    ) -> Decimal:
        """Return (bid + ask) / 2 from the transport's persistent L1 ticker.

        Raises MarketDataTimeoutError if no valid bid and ask arrive in time.
        """
        contract = await self._contract_resolver.resolve_from_pair(trading_pair)
        bid, ask = await self._transport.top_of_book(contract)
        mid: Decimal = (bid + ask) / 2
        return mid

    async def get_candles(
        self: HasIbTransport & HasContractResolver,  # type: ignore[valid-type]
        trading_pair: str,
        interval: str,
        limit: int,
    ) -> list[object]:  # returns list[CandleData]; typed loosely to avoid circular import
        """Fetch OHLCV candles via reqHistoricalDataAsync."""
        from market_connector.exchanges.interactive_brokers.interval_map import (  # noqa: PLC0415
            to_ib_bar_size,
        )
        from market_connector.exchanges.interactive_brokers.transport import (  # noqa: PLC0415
            _native,
        )
        from market_connector.primitives import CandleData  # noqa: PLC0415

        bar_size = to_ib_bar_size(interval)  # raises InvalidIntervalError early for bad intervals
        contract = await self._contract_resolver.resolve_from_pair(trading_pair)
        duration = _duration_string(interval, limit)

        bars = await self._transport._ib.reqHistoricalDataAsync(
            _native(contract),
            endDateTime="",
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow="TRADES",
            useRTH=False,
        )

        import calendar  # noqa: PLC0415
        import datetime  # noqa: PLC0415

        def _bar_timestamp(date_str: str) -> float:
            """Parse IB bar date string to epoch float."""
            try:
                # IB returns "YYYYMMDD HH:MM:SS" for intraday or "YYYYMMDD" for daily.
                if " " in date_str:
                    dt = datetime.datetime.strptime(date_str, "%Y%m%d %H:%M:%S")
                else:
                    dt = datetime.datetime.strptime(date_str, "%Y%m%d")
                return calendar.timegm(dt.timetuple())
            except ValueError:
                return 0.0

        result: list[CandleData] = []
        for bar in bars:
            vol = bar.volume
            result.append(
                CandleData(
                    trading_pair=trading_pair,
                    timestamp=_bar_timestamp(bar.date),
                    interval=interval,
                    open=Decimal(str(bar.open)),
                    high=Decimal(str(bar.high)),
                    low=Decimal(str(bar.low)),
                    close=Decimal(str(bar.close)),
                    volume=Decimal("0") if vol == -1 else Decimal(str(vol)),
                )
            )
        return cast("list[object]", result)
