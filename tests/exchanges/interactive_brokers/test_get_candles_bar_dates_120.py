"""Acceptance tests for get_candles bar.date handling (#120).

ib_async returns tz-aware ``datetime.datetime`` for intraday bars and ``datetime.date``
for daily bars; legacy string forms must keep parsing. Unparseable dates are skipped
with a logged warning (never emitted as timestamp 0.0).
"""

from __future__ import annotations

import datetime as dt
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from tests.exchanges.interactive_brokers.test_market_data_mixin import (
    ConcreteMarketDataHost,
    FakeResolver,
    FakeTransport,
)

ib_async = pytest.importorskip("ib_async")

UTC = dt.UTC


def _bar(date: object) -> object:
    """Build an ib_async-typed bar, falling back to a namespace with the same fields."""
    fields = {"open": 150.0, "high": 151.0, "low": 149.5, "close": 150.5, "volume": 1000.0}
    bar_cls = getattr(ib_async, "BarData", None)
    if bar_cls is not None:
        try:
            return bar_cls(date=date, **fields)
        except TypeError:
            pass
    return SimpleNamespace(date=date, **fields)


async def _candles(dates: list[object], interval: str = "1m") -> list[object]:
    transport = FakeTransport()
    transport._ib.reqHistoricalDataAsync = AsyncMock(return_value=[_bar(d) for d in dates])
    host = ConcreteMarketDataHost(transport, FakeResolver())
    return await host.get_candles("AAPL-USD", interval=interval, limit=5)


class TestBarDateTypes:
    @pytest.mark.asyncio
    async def test_tz_aware_utc_datetime(self) -> None:
        candles = await _candles([dt.datetime(2026, 10, 26, 13, 30, tzinfo=UTC)])
        assert [c.timestamp for c in candles] == [1793021400.0]

    @pytest.mark.asyncio
    async def test_tz_aware_non_utc_datetime(self) -> None:
        ny = dt.datetime(2026, 10, 26, 9, 30, tzinfo=ZoneInfo("America/New_York"))
        candles = await _candles([ny])
        assert [c.timestamp for c in candles] == [1793021400.0]  # 13:30 UTC (EDT)

    @pytest.mark.asyncio
    async def test_naive_datetime_treated_as_utc(self) -> None:
        candles = await _candles([dt.datetime(2026, 10, 26, 13, 30)])
        assert [c.timestamp for c in candles] == [1793021400.0]

    @pytest.mark.asyncio
    async def test_date_with_daily_interval_is_midnight_utc(self) -> None:
        candles = await _candles([dt.date(2026, 10, 26)], interval="1d")
        assert [c.timestamp for c in candles] == [1792972800.0]


class TestLegacyStringDates:
    @pytest.mark.asyncio
    async def test_intraday_string_still_parses(self) -> None:
        candles = await _candles(["20240101 09:30:00"])
        assert [c.timestamp for c in candles] == [1704101400.0]

    @pytest.mark.asyncio
    async def test_daily_string_still_parses(self) -> None:
        candles = await _candles(["20240101"], interval="1d")
        assert [c.timestamp for c in candles] == [1704067200.0]


class TestUnparseableDates:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["garbage", object()])
    async def test_bad_date_warns_and_bar_is_skipped(
        self, bad: object, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            candles = await _candles([bad])
        assert any(r.levelno >= logging.WARNING for r in caplog.records)
        assert candles == []
