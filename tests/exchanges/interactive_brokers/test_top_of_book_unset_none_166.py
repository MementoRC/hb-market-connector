"""Acceptance tests for issue #166: top_of_book must treat an unset (None) bid/ask like NaN.

ib_async 3.0 leaves an unset Ticker bid/ask as ``None``; 2.x used ``nan``. ``top_of_book``'s
``ready()`` compares the raw values with ``> 0``, which raises ``TypeError`` on ``None`` instead
of waiting. Each case runs on the 2.x MockIb rig and on the V3Ib rig (ib_async 3.0 wrapper).
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any

import pytest

from market_connector.exchanges.interactive_brokers.exceptions import MarketDataTimeoutError

from .test_ib_async_v3_compat_157 import _v3_rig
from .test_persistent_market_data import Rig, _call_later, _fire, _rig, _set_l1

_V3_PARAMS = pytest.mark.parametrize("v3", [False, True], ids=["ib_async_2x", "ib_async_3x"])


async def _rig_for(v3: bool, **spec_kwargs: Any) -> Rig:
    if v3:
        return await _v3_rig(**spec_kwargs)
    return _rig(**spec_kwargs)


@_V3_PARAMS
async def test_top_of_book_waits_while_bid_ask_none_then_returns(v3: bool) -> None:
    rig = await _rig_for(v3)
    ticker = rig.ib._l1_ticker
    ticker.bid = None
    ticker.ask = None
    contract = await rig.resolver.resolve_from_pair("AAPL-USD")

    def fill() -> None:
        _set_l1(ticker, 150.0, 150.5)
        _fire(ticker)

    _call_later(0.03, fill)
    bid, ask = await rig.transport.top_of_book(contract)
    assert (bid, ask) == (Decimal("150.0"), Decimal("150.5"))


@_V3_PARAMS
async def test_top_of_book_one_side_none_keeps_waiting(v3: bool) -> None:
    rig = await _rig_for(v3)
    ticker = rig.ib._l1_ticker
    ticker.bid = 150.0
    ticker.ask = None
    contract = await rig.resolver.resolve_from_pair("AAPL-USD")

    def fill_ask() -> None:
        ticker.ask = 150.5
        _fire(ticker)

    _call_later(0.03, fill_ask)
    bid, ask = await rig.transport.top_of_book(contract)
    assert (bid, ask) == (Decimal("150.0"), Decimal("150.5"))


@_V3_PARAMS
async def test_top_of_book_none_times_out_like_nan(v3: bool) -> None:
    rig = await _rig_for(v3, market_data_timeout=0.05)
    ticker = rig.ib._l1_ticker
    ticker.bid = None
    ticker.ask = None
    contract = await rig.resolver.resolve_from_pair("AAPL-USD")
    started = time.monotonic()
    with pytest.raises(MarketDataTimeoutError):
        await rig.transport.top_of_book(contract)
    assert time.monotonic() - started < 2.0


@_V3_PARAMS
async def test_top_of_book_nan_then_filled_still_returns(v3: bool) -> None:
    rig = await _rig_for(v3)
    ticker = rig.ib._l1_ticker
    contract = await rig.resolver.resolve_from_pair("AAPL-USD")

    def fill() -> None:
        _set_l1(ticker, 150.0, 150.5)
        _fire(ticker)

    _call_later(0.03, fill)
    bid, ask = await asyncio.wait_for(rig.transport.top_of_book(contract), timeout=2.0)
    assert (bid, ask) == (Decimal("150.0"), Decimal("150.5"))
