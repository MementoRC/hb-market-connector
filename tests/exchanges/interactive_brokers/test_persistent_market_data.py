"""Acceptance tests for persistent market data and L1 mid price (issue #54, criteria D1..D9).

Pinned decisions (beyond the issue text):
- Mid price comes from one persistent reqMktData ticker per contract; depth comes from one
  shared, ref-counted reqMktDepth stream per contract, released ``depth_idle_release`` seconds
  after its last use (snapshot or subscriber), never immediately.
- Idle-but-open depth streams (inside the idle window) are re-requested after a drop like any
  other open stream; released ones are not.
- "First depth update" means a book with at least one level on either side; an update that
  leaves both sides empty does not satisfy the wait.
- A timeout does not close the stream or ticker: a later call can still succeed on it.

Timing tests use small real values (asyncio, no internal patching); generous margins keep them
stable. NOTE: the real gateway does not set ``_contract_resolver`` (the mixins read it), so the
rig sets it explicitly, as the other mixin tests do.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from market_connector.exchanges.interactive_brokers._error_router import _ErrorRouter
from market_connector.exchanges.interactive_brokers.exceptions import (
    IbError,
    MarketDataTimeoutError,
)
from market_connector.exchanges.interactive_brokers.ib_gateway import IbGatewayGateway
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.transport import IbGatewayTransport

from ._doubles import FakeTicker, MockIb

if TYPE_CHECKING:
    from collections.abc import Callable

_IB_PATH = "market_connector.exchanges.interactive_brokers.transport.IB"
_NAN = float("nan")

# Idle-release values: short for "released" checks, long for "kept open" checks.
_IDLE_SHORT = 0.05
_IDLE_LONG = 0.2
_PAST_IDLE = 0.4

_BIDS = [SimpleNamespace(price=150.0, size=100.0), SimpleNamespace(price=149.5, size=200.0)]
_ASKS = [SimpleNamespace(price=150.5, size=50.0), SimpleNamespace(price=151.0, size=75.0)]
_EXPECTED_BIDS = [(Decimal("150.0"), Decimal("100.0")), (Decimal("149.5"), Decimal("200.0"))]
_EXPECTED_ASKS = [(Decimal("150.5"), Decimal("50.0")), (Decimal("151.0"), Decimal("75.0"))]


class FakeContract:
    """Hashable contract double: equal by conId, like ib_async contracts."""

    def __init__(self, con_id: int) -> None:
        self.conId = con_id

    def __eq__(self, other: object) -> bool:
        return isinstance(other, FakeContract) and other.conId == self.conId

    def __hash__(self) -> int:
        return hash(self.conId)

    def __repr__(self) -> str:
        return f"FakeContract({self.conId})"


class FakeResolver:
    """Resolver double: one stable contract per trading pair."""

    def __init__(self) -> None:
        self.contracts: dict[str, FakeContract] = {}

    async def resolve_from_pair(self, pair: str) -> FakeContract:
        return self.contracts.setdefault(pair, FakeContract(len(self.contracts) + 1))


@dataclass
class Rig:
    gateway: IbGatewayGateway
    transport: IbGatewayTransport
    ib: MockIb
    resolver: FakeResolver


def _rig(**spec_kwargs: Any) -> Rig:
    """Gateway over a real transport over MockIb; fast reconnects, no internal patching."""
    spec_kwargs.setdefault("reconnect_initial_delay", 0.01)
    spec_kwargs.setdefault("reconnect_max_delay", 0.01)
    ib = MockIb()
    with patch(_IB_PATH, return_value=ib):
        transport = IbGatewayTransport(IbConnectionSpec(**spec_kwargs))
    resolver = FakeResolver()
    gateway = IbGatewayGateway(transport=transport, signer=MagicMock(), contract_resolver=resolver)
    gateway._contract_resolver = resolver  # type: ignore[attr-defined]
    return Rig(gateway, transport, ib, resolver)


def _set_l1(ticker: FakeTicker, bid: float, ask: float) -> None:
    ticker.bid = bid
    ticker.ask = ask


def _populate_depth(ticker: FakeTicker) -> None:
    ticker.domBids = list(_BIDS)
    ticker.domAsks = list(_ASKS)


def _call_later(delay: float, fn: Callable[[], None]) -> None:
    asyncio.get_running_loop().call_later(delay, fn)


def _fire(ticker: FakeTicker) -> None:
    ticker.updateEvent(ticker)


# --------------------------------------------------------------------------- D1


class TestD1MidFromTopOfBook:
    async def test_d1_mid_is_average_of_l1_bid_ask(self) -> None:
        rig = _rig()
        _set_l1(rig.ib._l1_ticker, 150.0, 150.5)
        mid = await rig.gateway.get_mid_price("AAPL-USD")
        assert isinstance(mid, Decimal)
        assert mid == Decimal("150.25")

    async def test_d1_mid_uses_reqmktdata_never_reqmktdepth(self) -> None:
        rig = _rig()
        _set_l1(rig.ib._l1_ticker, 150.0, 150.5)
        await rig.gateway.get_mid_price("AAPL-USD")
        assert len(rig.ib.req_mkt_data_calls) == 1
        assert rig.ib.req_mkt_depth_calls == []

    async def test_d1_mid_is_exact_decimal_not_float_arithmetic(self) -> None:
        rig = _rig()
        _set_l1(rig.ib._l1_ticker, 0.1, 0.2)
        assert await rig.gateway.get_mid_price("AAPL-USD") == Decimal("0.15")

    async def test_d1_transport_top_of_book_returns_decimal_bid_ask(self) -> None:
        rig = _rig()
        _set_l1(rig.ib._l1_ticker, 150.0, 150.5)
        contract = await rig.resolver.resolve_from_pair("AAPL-USD")
        bid, ask = await rig.transport.top_of_book(contract)
        assert (bid, ask) == (Decimal("150.0"), Decimal("150.5"))
        assert isinstance(bid, Decimal)
        assert isinstance(ask, Decimal)

    @pytest.mark.parametrize(
        ("bid", "ask"),
        [(_NAN, 150.5), (150.0, _NAN), (0.0, 150.5), (150.0, -1.0), (-1.0, -1.0), (_NAN, _NAN)],
    )
    async def test_d1_unset_values_are_not_available(self, bid: float, ask: float) -> None:
        rig = _rig(market_data_timeout=0.05)
        _set_l1(rig.ib._l1_ticker, bid, ask)
        with pytest.raises(MarketDataTimeoutError):
            await rig.gateway.get_mid_price("AAPL-USD")


# --------------------------------------------------------------------------- D2


class TestD2OneL1TickerPerContract:
    async def test_d2_repeated_calls_issue_one_reqmktdata(self) -> None:
        rig = _rig()
        _set_l1(rig.ib._l1_ticker, 150.0, 150.5)
        for _ in range(3):
            await rig.gateway.get_mid_price("AAPL-USD")
        assert len(rig.ib.req_mkt_data_calls) == 1
        assert rig.ib.cancel_mkt_data_calls == []

    async def test_d2_each_pair_issues_one_reqmktdata(self) -> None:
        rig = _rig()
        _set_l1(rig.ib._l1_ticker, 150.0, 150.5)
        for pair in ("AAPL-USD", "MSFT-USD", "AAPL-USD", "MSFT-USD"):
            await rig.gateway.get_mid_price(pair)
        assert len(rig.ib.req_mkt_data_calls) == 2
        assert set(rig.ib.req_mkt_data_calls) == set(rig.resolver.contracts.values())

    async def test_d2_l1_tickers_stay_open_until_stop(self) -> None:
        rig = _rig()
        _set_l1(rig.ib._l1_ticker, 150.0, 150.5)
        await rig.gateway.start()
        await rig.gateway.get_mid_price("AAPL-USD")
        await rig.gateway.get_mid_price("MSFT-USD")
        await asyncio.sleep(_PAST_IDLE)
        assert rig.ib.cancel_mkt_data_calls == []
        await rig.gateway.stop()
        assert Counter(rig.ib.cancel_mkt_data_calls) == Counter(rig.resolver.contracts.values())


# --------------------------------------------------------------------------- D3


class TestD3NoFixedSleepForL1:
    async def test_d3_valid_values_return_immediately(self) -> None:
        rig = _rig()  # default 5.0s timeout: a fixed sleep or a wait would show up here
        _set_l1(rig.ib._l1_ticker, 150.0, 150.5)
        started = time.monotonic()
        await rig.gateway.get_mid_price("AAPL-USD")
        assert time.monotonic() - started < 0.5

    async def test_d3_waits_for_update_event_until_bid_and_ask_valid(self) -> None:
        rig = _rig()
        ticker = rig.ib._l1_ticker

        def deliver() -> None:
            _set_l1(ticker, 150.0, 150.5)
            _fire(ticker)

        _call_later(0.05, deliver)
        started = time.monotonic()
        mid = await rig.gateway.get_mid_price("AAPL-USD")
        elapsed = time.monotonic() - started
        assert mid == Decimal("150.25")
        assert 0.04 <= elapsed < 2.0

    async def test_d3_partial_update_keeps_waiting(self) -> None:
        rig = _rig()
        ticker = rig.ib._l1_ticker

        def bid_only() -> None:
            ticker.bid = 150.0
            _fire(ticker)

        def ask_too() -> None:
            ticker.ask = 150.5
            _fire(ticker)

        _call_later(0.03, bid_only)
        _call_later(0.10, ask_too)
        started = time.monotonic()
        mid = await rig.gateway.get_mid_price("AAPL-USD")
        assert mid == Decimal("150.25")
        assert time.monotonic() - started >= 0.09

    async def test_d3_timeout_raises_market_data_timeout_error(self) -> None:
        rig = _rig(market_data_timeout=0.05)
        started = time.monotonic()
        with pytest.raises(MarketDataTimeoutError):
            await rig.gateway.get_mid_price("AAPL-USD")
        assert time.monotonic() - started < 2.0

    async def test_d3_timeout_keeps_the_single_ticker_usable(self) -> None:
        rig = _rig(market_data_timeout=0.05)
        with pytest.raises(MarketDataTimeoutError):
            await rig.gateway.get_mid_price("AAPL-USD")
        _set_l1(rig.ib._l1_ticker, 150.0, 150.5)
        assert await rig.gateway.get_mid_price("AAPL-USD") == Decimal("150.25")
        assert len(rig.ib.req_mkt_data_calls) == 1

    def test_d3_timeout_error_is_an_ib_error(self) -> None:
        assert issubclass(MarketDataTimeoutError, IbError)
        assert MarketDataTimeoutError.__doc__

    def test_d3_spec_defaults(self) -> None:
        spec = IbConnectionSpec()
        assert spec.market_data_timeout == 5.0
        assert spec.depth_idle_release == 60.0

    @pytest.mark.parametrize("field", ["market_data_timeout", "depth_idle_release"])
    @pytest.mark.parametrize("value", [0, 0.0, -1.0])
    def test_d3_spec_rejects_non_positive(self, field: str, value: float) -> None:
        with pytest.raises(ValueError, match=field):
            IbConnectionSpec(**{field: value})

    def test_d3_spec_accepts_small_positive(self) -> None:
        spec = IbConnectionSpec(market_data_timeout=0.05, depth_idle_release=0.05)
        assert (spec.market_data_timeout, spec.depth_idle_release) == (0.05, 0.05)


# --------------------------------------------------------------------------- D4


class TestD4SharedRefCountedDepth:
    async def test_d4_get_orderbook_then_subscribe_share_one_stream(self) -> None:
        rig = _rig()
        _populate_depth(rig.ib._depth_ticker)
        await rig.gateway.get_orderbook("AAPL-USD")
        contract = rig.resolver.contracts["AAPL-USD"]
        handle = rig.transport.subscribe("depth", contract, lambda t: None)
        assert len(rig.ib.req_mkt_depth_calls) == 1
        handle.close()

    async def test_d4_subscribe_then_get_orderbook_share_one_stream(self) -> None:
        rig = _rig()
        _populate_depth(rig.ib._depth_ticker)
        contract = await rig.resolver.resolve_from_pair("AAPL-USD")
        handle = rig.transport.subscribe("depth", contract, lambda t: None)
        await rig.gateway.get_orderbook("AAPL-USD")
        assert len(rig.ib.req_mkt_depth_calls) == 1
        handle.close()

    async def test_d4_no_new_request_while_subscribed(self) -> None:
        rig = _rig()
        _populate_depth(rig.ib._depth_ticker)
        contract = await rig.resolver.resolve_from_pair("AAPL-USD")
        handle = rig.transport.subscribe("depth", contract, lambda t: None)
        for _ in range(3):
            await rig.gateway.get_orderbook("AAPL-USD")
        assert len(rig.ib.req_mkt_depth_calls) == 1
        assert rig.ib._cancel_mkt_depth_calls == []
        handle.close()

    async def test_d4_closing_one_of_two_handles_keeps_stream(self) -> None:
        rig = _rig(depth_idle_release=_IDLE_SHORT)
        contract = await rig.resolver.resolve_from_pair("AAPL-USD")
        seen_a: list[object] = []
        seen_b: list[object] = []
        first = rig.transport.subscribe("depth", contract, seen_a.append)
        second = rig.transport.subscribe("depth", contract, seen_b.append)
        assert len(rig.ib.req_mkt_depth_calls) == 1
        first.close()
        await asyncio.sleep(_PAST_IDLE)
        assert rig.ib._cancel_mkt_depth_calls == []
        _fire(rig.ib._depth_ticker)
        assert (len(seen_a), len(seen_b)) == (0, 1)
        second.close()

    async def test_d4_last_handle_close_cancels_after_idle_not_immediately(self) -> None:
        rig = _rig(depth_idle_release=_IDLE_SHORT)
        contract = await rig.resolver.resolve_from_pair("AAPL-USD")
        handle = rig.transport.subscribe("depth", contract, lambda t: None)
        handle.close()
        assert rig.ib._cancel_mkt_depth_calls == []
        await asyncio.sleep(_PAST_IDLE)
        assert rig.ib._cancel_mkt_depth_calls == [contract]

    async def test_d4_repeated_get_orderbook_within_window_one_request(self) -> None:
        rig = _rig(depth_idle_release=_IDLE_LONG)
        _populate_depth(rig.ib._depth_ticker)
        for _ in range(3):
            await rig.gateway.get_orderbook("AAPL-USD")
            await asyncio.sleep(0.02)
        assert len(rig.ib.req_mkt_depth_calls) == 1
        assert rig.ib._cancel_mkt_depth_calls == []

    async def test_d4_get_orderbook_alone_releases_after_idle_and_reopens(self) -> None:
        rig = _rig(depth_idle_release=_IDLE_SHORT)
        _populate_depth(rig.ib._depth_ticker)
        await rig.gateway.get_orderbook("AAPL-USD")
        assert rig.ib._cancel_mkt_depth_calls == []  # not cancelled on return
        await asyncio.sleep(_PAST_IDLE)
        contract = rig.resolver.contracts["AAPL-USD"]
        assert rig.ib._cancel_mkt_depth_calls == [contract]
        await rig.gateway.get_orderbook("AAPL-USD")
        assert len(rig.ib.req_mkt_depth_calls) == 2

    async def test_d4_use_resets_the_idle_period(self) -> None:
        rig = _rig(depth_idle_release=_IDLE_LONG)
        _populate_depth(rig.ib._depth_ticker)
        await rig.gateway.get_orderbook("AAPL-USD")
        await asyncio.sleep(0.12)
        await rig.gateway.get_orderbook("AAPL-USD")  # resets the period
        await asyncio.sleep(0.12)  # 0.24s since first use, 0.12s since the last
        assert rig.ib._cancel_mkt_depth_calls == []
        await asyncio.sleep(_PAST_IDLE)
        assert len(rig.ib._cancel_mkt_depth_calls) == 1
        assert len(rig.ib.req_mkt_depth_calls) == 1

    async def test_d4_subscribe_within_window_keeps_stream_open(self) -> None:
        rig = _rig(depth_idle_release=_IDLE_SHORT)
        _populate_depth(rig.ib._depth_ticker)
        await rig.gateway.get_orderbook("AAPL-USD")
        contract = rig.resolver.contracts["AAPL-USD"]
        handle = rig.transport.subscribe("depth", contract, lambda t: None)
        await asyncio.sleep(_PAST_IDLE)
        assert rig.ib._cancel_mkt_depth_calls == []
        assert len(rig.ib.req_mkt_depth_calls) == 1
        handle.close()
        await asyncio.sleep(_PAST_IDLE)
        assert rig.ib._cancel_mkt_depth_calls == [contract]


# --------------------------------------------------------------------------- D5


class TestD5OrderbookWaitsForFirstUpdate:
    async def test_d5_populated_book_returns_immediately_with_decimal_levels(self) -> None:
        rig = _rig()  # default 5.0s timeout
        _populate_depth(rig.ib._depth_ticker)
        started = time.monotonic()
        snapshot = await rig.gateway.get_orderbook("AAPL-USD")
        assert time.monotonic() - started < 0.5
        assert snapshot.trading_pair == "AAPL-USD"
        assert snapshot.bids == _EXPECTED_BIDS
        assert snapshot.asks == _EXPECTED_ASKS
        assert all(isinstance(v, Decimal) for lv in snapshot.bids + snapshot.asks for v in lv)

    async def test_d5_transport_depth_snapshot_returns_bids_and_asks(self) -> None:
        rig = _rig()
        _populate_depth(rig.ib._depth_ticker)
        contract = await rig.resolver.resolve_from_pair("AAPL-USD")
        bids, asks = await rig.transport.depth_snapshot(contract)
        assert (bids, asks) == (_EXPECTED_BIDS, _EXPECTED_ASKS)

    async def test_d5_empty_book_waits_for_update_event(self) -> None:
        rig = _rig()
        ticker = rig.ib._depth_ticker

        def deliver() -> None:
            _populate_depth(ticker)
            _fire(ticker)

        _call_later(0.05, deliver)
        started = time.monotonic()
        snapshot = await rig.gateway.get_orderbook("AAPL-USD")
        elapsed = time.monotonic() - started
        assert snapshot.bids == _EXPECTED_BIDS
        assert snapshot.asks == _EXPECTED_ASKS
        assert 0.04 <= elapsed < 2.0

    async def test_d5_update_that_leaves_book_empty_does_not_satisfy_wait(self) -> None:
        rig = _rig()
        ticker = rig.ib._depth_ticker

        def empty_update() -> None:
            _fire(ticker)

        def real_update() -> None:
            _populate_depth(ticker)
            _fire(ticker)

        _call_later(0.03, empty_update)
        _call_later(0.10, real_update)
        started = time.monotonic()
        snapshot = await rig.gateway.get_orderbook("AAPL-USD")
        assert time.monotonic() - started >= 0.09
        assert snapshot.bids == _EXPECTED_BIDS

    async def test_d5_no_update_raises_market_data_timeout_error(self) -> None:
        rig = _rig(market_data_timeout=0.05)
        started = time.monotonic()
        with pytest.raises(MarketDataTimeoutError):
            await rig.gateway.get_orderbook("AAPL-USD")
        assert time.monotonic() - started < 2.0


# --------------------------------------------------------------------------- D6


def _unrouted(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "unrouted" in r.getMessage().lower()]


class TestD6Code317DepthReset:
    _MSG = "Market depth data has been RESET. Please empty deep book contents."

    def test_d6_317_not_logged_as_unrouted(self, caplog: pytest.LogCaptureFixture) -> None:
        router = _ErrorRouter()
        with caplog.at_level(logging.DEBUG):
            router.on_error(1, 317, self._MSG)
        assert _unrouted(caplog) == []

    def test_d6_317_logged_at_info(self, caplog: pytest.LogCaptureFixture) -> None:
        router = _ErrorRouter()
        with caplog.at_level(logging.DEBUG):
            router.on_error(1, 317, self._MSG)
        records = [r for r in caplog.records if "317" in r.getMessage()]
        assert records
        assert all(r.levelno == logging.INFO for r in records)

    async def test_d6_317_fails_no_request_or_order_waiter(self) -> None:
        router = _ErrorRouter()
        loop = asyncio.get_running_loop()
        req_fut: asyncio.Future[Any] = loop.create_future()
        order_fut: asyncio.Future[Any] = loop.create_future()
        router._pending_request_waiters[1] = req_fut
        router._pending_order_waiters[1] = order_fut
        router.on_error(1, 317, self._MSG)
        router.on_error(-1, 317, self._MSG)
        assert not req_fut.done()
        assert not order_fut.done()
        assert router._pending_request_waiters == {1: req_fut}
        assert router._pending_order_waiters == {1: order_fut}

    @pytest.mark.parametrize("connected", [True, False])
    def test_d6_317_does_not_change_connection_state(self, connected: bool) -> None:
        router = _ErrorRouter()
        router.set_connected(connected)
        notified: list[bool] = []
        router._connection_listeners.append(notified.append)
        router.on_error(1, 317, self._MSG)
        assert router.is_connected is connected
        assert notified == []


# --------------------------------------------------------------------------- D7


class TestD7StopCancelsEverything:
    async def _open_everything(self, rig: Rig) -> tuple[FakeContract, FakeContract, FakeContract]:
        _set_l1(rig.ib._l1_ticker, 150.0, 150.5)
        _populate_depth(rig.ib._depth_ticker)
        await rig.gateway.start()
        await rig.gateway.get_mid_price("AAPL-USD")
        await rig.gateway.get_mid_price("MSFT-USD")
        await rig.gateway.get_orderbook("AAPL-USD")
        sub_contract = await rig.resolver.resolve_from_pair("NVDA-USD")
        rig.transport.subscribe("depth", sub_contract, lambda t: None)
        return (
            rig.resolver.contracts["AAPL-USD"],
            rig.resolver.contracts["MSFT-USD"],
            sub_contract,
        )

    async def test_d7_stop_cancels_every_l1_ticker_once(self) -> None:
        rig = _rig()
        aapl, msft, _ = await self._open_everything(rig)
        await rig.gateway.stop()
        assert Counter(rig.ib.cancel_mkt_data_calls) == Counter([aapl, msft])

    async def test_d7_stop_cancels_every_depth_stream_once_with_smart_depth(self) -> None:
        rig = _rig()
        aapl, _, nvda = await self._open_everything(rig)
        await rig.gateway.stop()
        assert Counter(rig.ib._cancel_mkt_depth_calls) == Counter([aapl, nvda])
        assert rig.ib.cancel_mkt_depth_smart == [True, True]

    async def test_d7_cancels_happen_before_disconnect(self) -> None:
        rig = _rig()
        await self._open_everything(rig)
        await rig.gateway.stop()
        log = rig.ib.event_log
        assert log.count("disconnect") == 1
        assert log[-1] == "disconnect"
        assert log.count("cancelMktData") == 2
        assert log.count("cancelMktDepth") == 2

    async def test_d7_no_idle_timer_fires_after_stop(self) -> None:
        rig = _rig(depth_idle_release=_IDLE_SHORT)
        _populate_depth(rig.ib._depth_ticker)
        await rig.gateway.start()
        await rig.gateway.get_orderbook("AAPL-USD")  # leaves an idle-release timer pending
        contract = await rig.resolver.resolve_from_pair("NVDA-USD")
        rig.transport.subscribe("depth", contract, lambda t: None).close()  # another pending timer
        await rig.gateway.stop()
        cancels_at_stop = list(rig.ib._cancel_mkt_depth_calls)
        await asyncio.sleep(_PAST_IDLE)
        assert rig.ib._cancel_mkt_depth_calls == cancels_at_stop
        assert max(Counter(rig.ib._cancel_mkt_depth_calls).values()) == 1

    async def test_d7_handle_close_after_stop_cancels_nothing_more(self) -> None:
        rig = _rig(depth_idle_release=_IDLE_SHORT)
        await rig.gateway.start()
        contract = await rig.resolver.resolve_from_pair("AAPL-USD")
        handle = rig.transport.subscribe("depth", contract, lambda t: None)
        await rig.gateway.stop()
        handle.close()
        await asyncio.sleep(_PAST_IDLE)
        assert rig.ib._cancel_mkt_depth_calls == [contract]

    async def test_d7_stop_with_nothing_open_cancels_nothing(self) -> None:
        rig = _rig()
        await rig.gateway.start()
        await rig.gateway.stop()
        assert rig.ib.cancel_mkt_data_calls == []
        assert rig.ib._cancel_mkt_depth_calls == []


# --------------------------------------------------------------------------- D8


async def _open_l1_and_depth(rig: Rig) -> FakeContract:
    """Connect, then open one L1 ticker and one idle-open depth stream for AAPL-USD."""
    _set_l1(rig.ib._l1_ticker, 150.0, 150.5)
    _populate_depth(rig.ib._depth_ticker)
    await rig.gateway.start()
    await rig.gateway.get_mid_price("AAPL-USD")
    await rig.gateway.get_orderbook("AAPL-USD")
    return rig.resolver.contracts["AAPL-USD"]


def _new_tickers_with_data(ib: MockIb) -> None:
    """Rotate to brand-new tickers carrying distinguishable data (mid 200.5, bids 200.0)."""
    ib.rotate_tickers()
    _set_l1(ib._l1_ticker, 200.0, 201.0)
    ib._depth_ticker.domBids = [SimpleNamespace(price=200.0, size=1.0)]
    ib._depth_ticker.domAsks = [SimpleNamespace(price=201.0, size=2.0)]


class TestD8SurviveDrops:
    async def test_d8_reconnect_rerequests_both_without_cancelling(self) -> None:
        rig = _rig(depth_idle_release=_IDLE_LONG * 10)
        await _open_l1_and_depth(rig)
        _new_tickers_with_data(rig.ib)

        rig.ib.simulate_drop()
        supervisor = rig.transport._supervisor
        assert supervisor is not None
        await supervisor

        assert len(rig.ib.req_mkt_data_calls) == 2
        assert len(rig.ib.req_mkt_depth_calls) == 2
        assert rig.ib.cancel_mkt_data_calls == []
        assert rig.ib._cancel_mkt_depth_calls == []
        await rig.gateway.stop()

    async def test_d8_reads_after_reconnect_use_the_new_tickers(self) -> None:
        rig = _rig(depth_idle_release=_IDLE_LONG * 10)
        await _open_l1_and_depth(rig)
        _new_tickers_with_data(rig.ib)

        rig.ib.simulate_drop()
        assert rig.transport._supervisor is not None
        await rig.transport._supervisor

        assert await rig.gateway.get_mid_price("AAPL-USD") == Decimal("200.5")
        snapshot = await rig.gateway.get_orderbook("AAPL-USD")
        assert snapshot.bids == [(Decimal("200.0"), Decimal("1.0"))]
        assert snapshot.asks == [(Decimal("201.0"), Decimal("2.0"))]
        assert len(rig.ib.req_mkt_data_calls) == 2
        assert len(rig.ib.req_mkt_depth_calls) == 2
        await rig.gateway.stop()

    async def test_d8_router_1101_cancels_then_rerequests_both(self) -> None:
        rig = _rig(depth_idle_release=_IDLE_LONG * 10)
        contract = await _open_l1_and_depth(rig)
        _new_tickers_with_data(rig.ib)

        rig.ib.errorEvent(-1, 1101, "data lost")

        assert rig.ib.cancel_mkt_data_calls == [contract]
        assert rig.ib._cancel_mkt_depth_calls == [contract]
        assert len(rig.ib.req_mkt_data_calls) == 2
        assert len(rig.ib.req_mkt_depth_calls) == 2
        await rig.gateway.stop()

    async def test_d8_reads_after_1101_use_the_new_tickers(self) -> None:
        rig = _rig(depth_idle_release=_IDLE_LONG * 10)
        await _open_l1_and_depth(rig)
        _new_tickers_with_data(rig.ib)

        rig.ib.errorEvent(-1, 1101, "data lost")

        assert await rig.gateway.get_mid_price("AAPL-USD") == Decimal("200.5")
        snapshot = await rig.gateway.get_orderbook("AAPL-USD")
        assert snapshot.bids == [(Decimal("200.0"), Decimal("1.0"))]
        await rig.gateway.stop()

    async def test_d8_1102_alone_rerequests_nothing(self) -> None:
        rig = _rig(depth_idle_release=_IDLE_LONG * 10)
        await _open_l1_and_depth(rig)
        rig.ib.errorEvent(-1, 1100, "lost")
        rig.ib.errorEvent(-1, 1102, "restored")
        assert len(rig.ib.req_mkt_data_calls) == 1
        assert len(rig.ib.req_mkt_depth_calls) == 1
        assert rig.ib.cancel_mkt_data_calls == []
        await rig.gateway.stop()


# --------------------------------------------------------------------------- D9


class TestD9SmartDepthEverywhere:
    async def test_d9_every_reqmktdepth_and_cancel_uses_smart_depth(self) -> None:
        rig = _rig(depth_idle_release=_IDLE_SHORT)
        contract = await _open_l1_and_depth(rig)
        handle = rig.transport.subscribe("depth", contract, lambda t: None)
        rig.ib.errorEvent(-1, 1101, "data lost")  # cancel + re-request
        rig.ib.simulate_drop()  # reconnect: re-request
        assert rig.transport._supervisor is not None
        await rig.transport._supervisor
        handle.close()
        await asyncio.sleep(_PAST_IDLE)  # idle release cancels
        await rig.gateway.stop()

        assert len(rig.ib.req_mkt_depth_calls) >= 3
        assert len(rig.ib.req_mkt_depth_smart) == len(rig.ib.req_mkt_depth_calls)
        assert all(flag is True for flag in rig.ib.req_mkt_depth_smart)
        assert rig.ib._cancel_mkt_depth_calls
        assert len(rig.ib.cancel_mkt_depth_smart) == len(rig.ib._cancel_mkt_depth_calls)
        assert all(flag is True for flag in rig.ib.cancel_mkt_depth_smart)


# Expected ladder after each step, as (price, size) rows, for ib_async#239. IB semantics
# (TWS API market depth): insert shifts rows at/after the position down by one; update
# overwrites the row; delete removes it and shifts later rows up.
def _apply_reference(
    book: list[tuple[float, float]], operation: int, position: int, price: float, size: float
) -> None:
    if operation == 0:
        book.insert(position, (price, size))
    elif operation == 1:
        book[position] = (price, size)
    else:
        del book[position]


_DEPTH_OPS: list[tuple[int, int, int, float, float]] = [
    # (side, operation, position, price, size); side 1 = bid, 0 = ask
    (1, 0, 0, 100.0, 10.0),
    (1, 0, 1, 99.0, 20.0),
    (1, 0, 2, 98.0, 30.0),
    (0, 0, 0, 101.0, 11.0),
    (0, 0, 1, 102.0, 21.0),
    (0, 0, 2, 103.0, 31.0),
    (1, 0, 0, 100.5, 15.0),  # new best bid: old rows shift down
    (1, 1, 1, 100.0, 12.0),  # update in place
    (1, 2, 0, 100.5, 0.0),  # delete best bid: rows shift up
    (0, 0, 1, 101.5, 5.0),  # insert in the middle of the asks
    (0, 2, 2, 102.0, 0.0),  # delete from the middle
    (1, 2, 2, 98.0, 0.0),  # delete the last bid
]


@pytest.mark.xfail(
    strict=True,
    reason="ib_async#239: Wrapper.updateMktDepthL2 overwrites dict slots instead of "
    "shifting rows on insert/delete (and lists them in insertion order)",
)
async def test_d9_real_ib_async_depth_rows_stay_consistent() -> None:
    """D9 guard: drive ib_async's REAL Wrapper.updateMktDepthL2 for a smart-depth ticker."""
    ib_async = pytest.importorskip("ib_async")
    ib = ib_async.IB()
    ticker = ib_async.Ticker()
    ib.wrapper.reqId2Ticker[1] = ticker

    bids: list[tuple[float, float]] = []
    asks: list[tuple[float, float]] = []
    for step, (side, operation, position, price, size) in enumerate(_DEPTH_OPS):
        ib.wrapper.updateMktDepthL2(1, position, "", operation, side, price, size, True)
        reference = bids if side else asks
        _apply_reference(reference, operation, position, price, size)
        actual_bids = [(lv.price, lv.size) for lv in ticker.domBids]
        actual_asks = [(lv.price, lv.size) for lv in ticker.domAsks]
        assert actual_bids == bids, f"bids diverged at step {step}: {_DEPTH_OPS[step]}"
        assert actual_asks == asks, f"asks diverged at step {step}: {_DEPTH_OPS[step]}"
        assert len({p for p, _ in actual_bids}) == len(actual_bids)
        assert len({p for p, _ in actual_asks}) == len(actual_asks)
