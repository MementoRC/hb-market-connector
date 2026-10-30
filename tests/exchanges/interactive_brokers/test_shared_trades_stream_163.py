"""Acceptance tests for issue #163: trades subscribers share one tick-by-tick stream per contract.

Like depth and L1, N trades subscribers on one contract must cost one ``reqTickByTickData``, one
market-data line and one reqId. Errors routed by that reqId reach every subscriber, closing a
subscriber keeps the stream live for the others, and the last close cancels it exactly once.
Different contracts keep separate streams.

Run against the ib_async 2.x double (``MockIb``) and a 3.0 double (``V3Ib``) that also mimics
3.0 idempotency: a repeated ``reqTickByTickData`` for the same contract and tick type returns
the existing ticker without drawing a new reqId.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from market_connector.exchanges.interactive_brokers.exceptions import (
    MarketDataLinesExhaustedError,
)
from market_connector.exchanges.interactive_brokers.ib_gateway import IbGatewayGateway
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.transport import IbGatewayTransport

from ._doubles import FakeTicker, MockIb
from .test_ib_async_v3_compat_157 import V3Ib
from .test_market_data_real_contract import _resolved
from .test_persistent_market_data import FakeResolver, Rig

_IB_PATH = "market_connector.exchanges.interactive_brokers.transport.IB"
_MSG_101 = "Max number of tickers has been reached"
_PROMPT = 2.0


class V3IdempotentIb(V3Ib):
    """V3Ib that, like ib_async 3.0, returns the live ticker for a repeated tick-by-tick request."""

    def __init__(self) -> None:
        super().__init__()
        self._live: dict[tuple[Any, str], FakeTicker] = {}

    def reqTickByTickData(  # noqa: N802
        self,
        contract: Any,
        tickType: str,  # noqa: N803
        ignoreSize: int,  # noqa: N803
        useRTH: bool,  # noqa: N803
    ) -> FakeTicker:
        self.req_tick_by_tick_calls.append(contract)
        key = (getattr(contract, "conId", contract), tickType)
        if key in self._live:
            return self._live[key]
        ticker = self._tick_ticker if not self._live else FakeTicker()
        self._live[key] = ticker
        self._tick_req_ids.append(self._start_ticker("tickByTick", ticker))
        return ticker

    def cancelTickByTickData(self, contract: Any, tickType: str) -> None:  # noqa: N802, N803
        super().cancelTickByTickData(contract, tickType)
        self._live.pop((getattr(contract, "conId", contract), tickType), None)


async def _rig(ib_cls: type[MockIb], **spec_kwargs: Any) -> Rig:
    spec_kwargs.setdefault("reconnect_initial_delay", 0.01)
    spec_kwargs.setdefault("reconnect_max_delay", 0.01)
    spec_kwargs.setdefault("market_data_timeout", 30.0)
    spec_kwargs.setdefault("market_data_lines", 5)
    ib = ib_cls()
    with patch(_IB_PATH, return_value=ib):
        transport = IbGatewayTransport(IbConnectionSpec(**spec_kwargs))
    resolver = FakeResolver()
    gateway = IbGatewayGateway(transport=transport, signer=MagicMock(), contract_resolver=resolver)
    rig = Rig(gateway, transport, ib, resolver)
    await rig.gateway.start()
    return rig


@pytest.fixture(params=[MockIb, V3IdempotentIb], ids=["ib_async_2x", "ib_async_3_0"])
async def rig(request: pytest.FixtureRequest) -> Rig:
    return await _rig(request.param)


def _noop(_data: Any) -> None:
    return None


def _emit_trade(ticker: FakeTicker, tick: object) -> None:
    ticker.tickByTicks.append(tick)
    ticker.updateEvent(ticker)


async def _wait_until(predicate: Any) -> None:
    for _ in range(int(_PROMPT / 0.005)):
        if predicate():
            return
        await asyncio.sleep(0.005)


async def test_second_trades_subscriber_issues_no_new_request_and_takes_no_line(rig: Rig) -> None:
    rig.transport.subscribe("trades", _resolved(1), _noop)
    assert len(rig.ib.req_tick_by_tick_calls) == 1
    assert rig.transport.market_data_lines_in_use == 1

    rig.transport.subscribe("trades", _resolved(1), _noop)

    assert len(rig.ib.req_tick_by_tick_calls) == 1, "second subscriber re-requested the stream"
    assert len(rig.ib._tick_req_ids) == 1
    assert rig.transport.market_data_lines_in_use == 1


async def test_every_subscriber_of_the_shared_stream_receives_each_trade(rig: Rig) -> None:
    got_a: list[object] = []
    got_b: list[object] = []
    rig.transport.subscribe("trades", _resolved(1), got_a.append)
    rig.transport.subscribe("trades", _resolved(1), got_b.append)
    assert len(rig.ib.req_tick_by_tick_calls) == 1, "stream not shared"

    tick = SimpleNamespace(price=1.0)
    _emit_trade(rig.ib._tick_ticker, tick)

    assert got_a == [tick]
    assert got_b == [tick]


async def test_stream_error_reaches_every_subscriber(rig: Rig) -> None:
    errors_a: list[Exception] = []
    errors_b: list[Exception] = []
    handle_a = rig.transport.subscribe("trades", _resolved(1), _noop, on_error=errors_a.append)
    handle_b = rig.transport.subscribe("trades", _resolved(1), _noop, on_error=errors_b.append)
    assert len(rig.ib._tick_req_ids) == 1, "stream not shared: one reqId expected"

    rig.ib.errorEvent(rig.ib._tick_req_ids[0], 101, _MSG_101, None)
    await _wait_until(lambda: handle_a.closed and handle_b.closed)

    assert isinstance(handle_a.error, MarketDataLinesExhaustedError), repr(handle_a.error)
    assert isinstance(handle_b.error, MarketDataLinesExhaustedError), repr(handle_b.error)
    assert errors_a == [handle_a.error]
    assert errors_b == [handle_b.error]
    assert rig.transport.market_data_lines_in_use == 0
    assert len(rig.ib._cancel_tick_by_tick_calls) == 1


async def test_closing_one_of_two_subscribers_keeps_the_stream_live(rig: Rig) -> None:
    got_a: list[object] = []
    got_b: list[object] = []
    handle_a = rig.transport.subscribe("trades", _resolved(1), got_a.append)
    rig.transport.subscribe("trades", _resolved(1), got_b.append)

    handle_a.close()

    assert rig.ib._cancel_tick_by_tick_calls == [], "stream cancelled while a subscriber remains"
    assert rig.transport.market_data_lines_in_use == 1
    tick = SimpleNamespace(price=2.0)
    _emit_trade(rig.ib._tick_ticker, tick)
    assert got_b == [tick]
    assert got_a == []


async def test_closing_the_last_subscriber_cancels_once_and_frees_one_line(rig: Rig) -> None:
    handle_a = rig.transport.subscribe("trades", _resolved(1), _noop)
    handle_b = rig.transport.subscribe("trades", _resolved(1), _noop)

    handle_a.close()
    handle_b.close()
    handle_b.close()  # idempotent

    assert len(rig.ib._cancel_tick_by_tick_calls) == 1
    assert rig.transport.market_data_lines_in_use == 0
    assert rig.ib._tick_ticker.updateEvent._handlers == []


async def test_different_contracts_get_separate_streams(rig: Rig) -> None:
    handle_1 = rig.transport.subscribe("trades", _resolved(1), _noop)
    rig.transport.subscribe("trades", _resolved(2), _noop)

    assert len(rig.ib.req_tick_by_tick_calls) == 2
    assert rig.transport.market_data_lines_in_use == 2

    handle_1.close()

    assert len(rig.ib._cancel_tick_by_tick_calls) == 1
    assert rig.transport.market_data_lines_in_use == 1
