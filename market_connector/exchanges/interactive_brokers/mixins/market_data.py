"""MarketDataMixin: get_orderbook, get_mid_price, get_candles for IB gateway."""

from __future__ import annotations

import datetime
import logging
import time
from decimal import Decimal
from typing import TYPE_CHECKING, cast

from market_connector.exchanges.interactive_brokers.mixins._targets import resolve_target

if TYPE_CHECKING:
    from market_connector.contracts.instrument import InstrumentRef
    from market_connector.exchanges.interactive_brokers.mixins.protocols import (  # noqa: F401
        HasContractResolver,
        HasIbTransport,
    )
    from market_connector.primitives import CandleData, ConnectorPair

logger = logging.getLogger(__name__)

_INTERVAL_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}


def _bar_epoch(value: object) -> float | None:
    """Convert an IB bar date to a UTC epoch, or None if unparseable.

    ib_async yields a tz-aware datetime (intraday) or date (daily); the legacy
    wire format is "YYYYMMDD HH:MM:SS" / "YYYYMMDD". Naive values are UTC.
    """
    # datetime is a subclass of date, so it must be checked first.
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.UTC)
        return value.timestamp()
    if isinstance(value, datetime.date):
        return datetime.datetime(
            value.year, value.month, value.day, tzinfo=datetime.UTC
        ).timestamp()
    if isinstance(value, str):
        fmt = "%Y%m%d %H:%M:%S" if " " in value else "%Y%m%d"
        try:
            parsed = datetime.datetime.strptime(value, fmt).replace(tzinfo=datetime.UTC)
        except ValueError:
            return None
        return parsed.timestamp()
    return None


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
        trading_pair: InstrumentRef | ConnectorPair,
    ) -> object:  # returns OrderBookSnapshot; typed loosely to avoid circular import
        """Snapshot DOM depth from the transport's shared depth stream.

        Accepts an InstrumentRef for non-stock asset classes (crypto/forex/futures);
        pair strings still resolve as STOCK. The snapshot label is "SYMBOL-QUOTE" for
        an InstrumentRef with a quote currency, the bare symbol otherwise, and the
        pair string unchanged.
        """
        contract, label = await resolve_target(trading_pair, self._contract_resolver)  # type: ignore[attr-defined]
        bids, asks = await self._transport.depth_snapshot(contract)  # type: ignore[attr-defined]

        # Late import to avoid circular dependency with framework.
        from market_connector.primitives import OrderBookSnapshot  # noqa: PLC0415

        return OrderBookSnapshot(
            trading_pair=label,
            bids=bids,
            asks=asks,
            timestamp=time.time(),
        )

    async def get_mid_price(
        self: HasIbTransport & HasContractResolver,  # type: ignore[valid-type]
        trading_pair: InstrumentRef | ConnectorPair,
    ) -> Decimal:
        """Return (bid + ask) / 2 from the transport's persistent L1 ticker.

        Accepts an InstrumentRef for non-stock asset classes (crypto/forex/futures);
        pair strings still resolve as STOCK.
        Raises MarketDataTimeoutError if no valid bid and ask arrive in time.
        """
        contract, _ = await resolve_target(trading_pair, self._contract_resolver)
        bid, ask = await self._transport.top_of_book(contract)
        mid: Decimal = (bid + ask) / 2
        return mid

    async def get_candles(
        self: HasIbTransport & HasContractResolver,  # type: ignore[valid-type]
        trading_pair: InstrumentRef | ConnectorPair,
        interval: str,
        limit: int,
    ) -> list[object]:  # returns list[CandleData]; typed loosely to avoid circular import
        """Fetch OHLCV candles via reqHistoricalDataAsync.

        Accepts an InstrumentRef for non-stock asset classes (crypto/forex/futures);
        pair strings still resolve as STOCK. Candle label is "SYMBOL-QUOTE" for an
        InstrumentRef with a quote currency, the bare symbol otherwise, and the pair
        string unchanged.
        """
        from market_connector.exchanges.interactive_brokers.interval_map import (  # noqa: PLC0415
            to_ib_bar_size,
        )
        from market_connector.exchanges.interactive_brokers.transport import (  # noqa: PLC0415
            _native,
        )
        from market_connector.primitives import CandleData  # noqa: PLC0415

        bar_size = to_ib_bar_size(interval)  # raises InvalidIntervalError early for bad intervals
        contract, label = await resolve_target(trading_pair, self._contract_resolver)
        duration = _duration_string(interval, limit)

        ib_contract = _native(contract)
        end_time, what_to_show, use_rth = "", "TRADES", False
        con_id = getattr(ib_contract, "conId", 0)
        exchange = getattr(ib_contract, "exchange", "")
        contract_key = con_id or (
            getattr(ib_contract, "symbol", ""),
            getattr(ib_contract, "secType", ""),
            exchange,
            getattr(ib_contract, "currency", ""),
        )
        request_key = (contract_key, end_time, duration, bar_size, what_to_show, use_rth)
        async with self._transport.pacer.historical(
            contract_key=contract_key,
            exchange=exchange,
            what_to_show=what_to_show,
            bar_size=bar_size,
            request_key=request_key,
        ):
            bars = await self._transport._ib.reqHistoricalDataAsync(
                ib_contract,
                endDateTime=end_time,
                durationStr=duration,
                barSizeSetting=bar_size,
                whatToShow=what_to_show,
                useRTH=use_rth,
            )

        result: list[CandleData] = []
        for bar in bars:
            ts = _bar_epoch(bar.date)
            if ts is None:
                logger.warning("Skipping IB bar with unparseable date: %r", bar.date)
                continue
            vol = bar.volume
            result.append(
                CandleData(
                    trading_pair=label,
                    timestamp=ts,
                    interval=interval,
                    open=Decimal(str(bar.open)),
                    high=Decimal(str(bar.high)),
                    low=Decimal(str(bar.low)),
                    close=Decimal(str(bar.close)),
                    volume=Decimal("0") if vol == -1 else Decimal(str(vol)),
                )
            )
        return cast("list[object]", result)
