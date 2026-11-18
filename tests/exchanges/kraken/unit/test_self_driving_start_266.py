"""Self-driving ``KrakenConnectorCore.start()`` (issue #266), RED step.

Pinned contract (nothing here exists yet):

* New keyword-only ``KrakenConnectorCore.__init__`` options: ``trading_rules_refresh_interval``
  (1800.0), ``balance_refresh_interval`` / ``price_refresh_interval`` (``None`` -> the
  ``status_poll_interval``), ``own_trades_ws`` (True), ``ws_reconnect_delay`` (1.0) and
  ``ws_reconnect_delay_max`` (60.0, exponential backoff between resubscribe attempts).
* ``track_pairs`` / ``untrack_pairs`` / ``tracked_pairs`` (frozenset); the two mutators are also
  part of ``ConnectorCoreProtocol``.
* ``KrakenGateway.wait_ws_disconnect()``: returns when the private WS connection drops. The
  core's own-trades supervisor enters ``gateway.subscribe_own_trades(self.on_own_trades)``,
  marks the user stream up, awaits ``wait_ws_disconnect()``, marks it down, backs off and
  resubscribes. A failure entering the subscription is backed off and retried too.
* ``start()`` alone: rules refresh (now, then every interval), balance refresh (now, then every
  interval), price refresh every interval over tracked pairs plus pairs of in-flight orders
  (never order books), the REST order-status poll, and the supervisor when ``own_trades_ws``.
  A refresh failure is logged and retried on the next tick; loops are independent.
* ``status_dict`` gains ``user_stream_initialized`` only when ``own_trades_ws`` is True.
* ``stop()`` cancels everything (the subscription is exited) within ``stop_grace``.

Only the REST transport and the WS subscription/disconnect seams are faked.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import inspect
import time
from collections import defaultdict
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from data_type_primitives.common import OrderType, PriceType

from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway
from market_connector.hb_compat.core_protocol import ConnectorCoreProtocol
from market_connector.hb_compat.event_bus import EventBus
from market_connector.transport.ws_base import WsConnectorBase
from tests.exchanges.kraken.unit import test_kraken_connector_core_211 as base
from tests.unit.transport import test_ws_base as wsb

if TYPE_CHECKING:
    from collections.abc import Callable

_CORE_MOD = "market_connector.exchanges.kraken.hb_compat.kraken_connector_core"

KEYS_4 = {
    "symbols_mapping_initialized",
    "trading_rule_initialized",
    "account_balance",
    "status_polling_initialized",
}
KEYS_5 = KEYS_4 | {"user_stream_initialized"}
_USER_STREAM = "user_stream_initialized"
_FOREVER = 10**6
_BTC = "BTC-USD"
_ETH = "ETH-USD"


# ---------------------------------------------------------------------------
# Fakes and helpers
# ---------------------------------------------------------------------------


async def _until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_poll(), timeout)


class Probe:
    """Wraps the gateway REST reads: records calls, injects failures, can hang."""

    NAMES = ("get_asset_pairs", "get_balances_ex", "get_ticker", "get_orderbook")

    def __init__(self, gateway: KrakenGateway, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: dict[str, list[Any]] = defaultdict(list)
        self.fail: dict[str, int] = defaultdict(int)  # name -> next N calls raise
        self.hang: set[str] = set()
        for name in self.NAMES:
            monkeypatch.setattr(gateway, name, self._wrap(name, getattr(gateway, name)))

    def _wrap(self, name: str, real: Callable[..., Any]) -> Callable[..., Any]:
        async def _wrapped(*args: Any, **kwargs: Any) -> Any:
            self.calls[name].append(args[0] if args else next(iter(kwargs.values()), None))
            if name in self.hang:
                await asyncio.Event().wait()
            if self.fail[name] > 0:
                self.fail[name] -= 1
                raise httpx.ConnectError("down")
            return await real(*args, **kwargs)

        return _wrapped

    def count(self, name: str) -> int:
        return len(self.calls[name])

    @property
    def tickers(self) -> list[Any]:
        return self.calls["get_ticker"]


class _Subscription:
    def __init__(self, ws: FakeOwnTradesWs) -> None:
        self._ws = ws

    async def __aenter__(self) -> None:
        self._ws.attempts += 1
        self._ws.attempt_times.append(time.monotonic())
        if self._ws.fail_enters > 0:
            self._ws.fail_enters -= 1
            raise ConnectionError("subscribe failed")
        self._ws.enters += 1
        self._ws.active += 1

    async def __aexit__(self, *_: Any) -> None:
        self._ws.exits += 1
        self._ws.active -= 1


class FakeOwnTradesWs:
    """Scriptable stand-in for ``subscribe_own_trades`` / ``wait_ws_disconnect``."""

    def __init__(self) -> None:
        self.fail_enters = 0  # next N subscription attempts raise on enter
        self.attempts = 0
        self.attempt_times: list[float] = []
        self.enters = 0  # successful enters
        self.exits = 0
        self.active = 0
        self.callback: Callable[[Any], None] | None = None
        self.disconnect_waits = 0
        self._drop = asyncio.Event()

    async def subscribe_own_trades(self, callback: Callable[[Any], None]) -> _Subscription:
        self.callback = callback
        return _Subscription(self)

    async def wait_ws_disconnect(self, generation: int | None = None) -> None:  # noqa: ARG002
        self.disconnect_waits += 1
        await self._drop.wait()
        self._drop = asyncio.Event()  # re-arm for the next connection

    def drop(self) -> None:
        self._drop.set()

    def push(self, event: Any) -> None:
        assert self.callback is not None
        self.callback(event)


@pytest.fixture
def rest() -> base.FakeKrakenRest:
    return base.FakeKrakenRest()


@pytest.fixture
def gateway(rest: base.FakeKrakenRest) -> KrakenGateway:
    gw = KrakenGateway(api_key="k", secret_key="c2VjcmV0")
    gw._rest = rest  # type: ignore[assignment]
    gw._started = True
    return gw


@pytest.fixture
def probe(gateway: KrakenGateway, monkeypatch: pytest.MonkeyPatch) -> Probe:
    return Probe(gateway, monkeypatch)


@pytest.fixture
def ws(gateway: KrakenGateway, monkeypatch: pytest.MonkeyPatch) -> FakeOwnTradesWs:
    fake = FakeOwnTradesWs()
    monkeypatch.setattr(gateway, "subscribe_own_trades", fake.subscribe_own_trades)
    monkeypatch.setattr(gateway, "wait_ws_disconnect", fake.wait_ws_disconnect, raising=False)
    return fake


@pytest.fixture
def bus() -> EventBus:
    return EventBus(name="core-266")


@pytest.fixture
def seen(bus: EventBus) -> base.Recorder:
    return base.Recorder(bus)


@pytest.fixture
async def build(
    gateway: KrakenGateway,
    bus: EventBus,
    seen: base.Recorder,
    probe: Probe,
    ws: FakeOwnTradesWs,
) -> Any:
    """Core factory; every refresh interval defaults to 10s so a test enables only what it needs."""
    cls = importlib.import_module(_CORE_MOD).KrakenConnectorCore
    cores: list[Any] = []

    def _build(**options: Any) -> Any:
        opts: dict[str, Any] = {
            "status_poll_interval": 10.0,
            "trading_rules_refresh_interval": 10.0,
            "balance_refresh_interval": 10.0,
            "price_refresh_interval": 10.0,
            "ws_reconnect_delay": 0.01,
            "ws_reconnect_delay_max": 0.05,
            "stop_grace": 1.0,
        }
        opts.update(options)
        core = cls(gateway, event_bus=bus, **opts)
        cores.append(core)
        return core

    yield _build
    for core in cores:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(core.stop(), 5.0)


# ---------------------------------------------------------------------------
# 1. Refresh cadence
# ---------------------------------------------------------------------------


class TestRulesAndBalanceRefresh:
    async def test_start_alone_loads_rules_and_balances(self, build: Any, probe: Probe) -> None:
        core = build()
        await core.start()
        await _until(lambda: probe.count("get_asset_pairs") >= 1)
        await _until(lambda: probe.count("get_balances_ex") >= 1)
        await _until(lambda: _BTC in core.trading_rules)
        await _until(lambda: core.status_dict["account_balance"])
        assert core.status_dict["trading_rule_initialized"] is True
        assert core.status_dict["symbols_mapping_initialized"] is True

    async def test_rules_refresh_repeats_at_its_interval(self, build: Any, probe: Probe) -> None:
        core = build(trading_rules_refresh_interval=0.02)
        await core.start()
        await _until(lambda: probe.count("get_asset_pairs") >= 3)

    async def test_long_rules_interval_runs_once_while_balances_repeat(
        self, build: Any, probe: Probe
    ) -> None:
        core = build(trading_rules_refresh_interval=10.0, balance_refresh_interval=0.02)
        await core.start()
        await _until(lambda: probe.count("get_balances_ex") >= 3)
        await asyncio.sleep(0.2)
        assert probe.count("get_asset_pairs") == 1

    async def test_balance_interval_none_follows_status_poll_interval(
        self, build: Any, probe: Probe
    ) -> None:
        core = build(status_poll_interval=0.02, balance_refresh_interval=None)
        await core.start()
        await _until(lambda: probe.count("get_balances_ex") >= 3)

    async def test_explicit_balance_interval_overrides_status_poll_interval(
        self, build: Any, probe: Probe
    ) -> None:
        core = build(status_poll_interval=0.02, balance_refresh_interval=10.0)
        await core.start()
        await _until(lambda: probe.count("get_balances_ex") >= 1)
        await asyncio.sleep(0.3)
        assert probe.count("get_balances_ex") == 1

    async def test_long_balance_interval_runs_once_while_rules_repeat(
        self, build: Any, probe: Probe
    ) -> None:
        core = build(trading_rules_refresh_interval=0.02, balance_refresh_interval=10.0)
        await core.start()
        await _until(lambda: probe.count("get_asset_pairs") >= 3)
        await asyncio.sleep(0.2)
        assert probe.count("get_balances_ex") == 1

    def test_defaults_are_the_agreed_values(self) -> None:
        cls = importlib.import_module(_CORE_MOD).KrakenConnectorCore
        params = inspect.signature(cls.__init__).parameters
        expected = {
            "trading_rules_refresh_interval": 1800.0,
            "balance_refresh_interval": None,
            "price_refresh_interval": None,
            "own_trades_ws": True,
            "ws_reconnect_delay": 1.0,
            "ws_reconnect_delay_max": 60.0,
        }
        for name, default in expected.items():
            assert name in params, name
            assert params[name].kind is inspect.Parameter.KEYWORD_ONLY, name
            assert params[name].default == default, name


# ---------------------------------------------------------------------------
# 2. Price refresh scope
# ---------------------------------------------------------------------------


class TestPriceRefresh:
    async def test_tracked_pair_prices_refresh_repeatedly(self, build: Any, probe: Probe) -> None:
        core = build(price_refresh_interval=0.02)
        core.track_pairs([_BTC])
        await core.start()
        await _until(lambda: probe.tickers.count("XBTUSD") >= 3)
        await _until(lambda: not core.get_price_by_type(_BTC, PriceType.MidPrice).is_nan())
        assert core.get_price_by_type(_BTC, PriceType.MidPrice) == Decimal("27005.0")

    async def test_price_interval_none_follows_status_poll_interval(
        self, build: Any, probe: Probe
    ) -> None:
        core = build(status_poll_interval=0.02, price_refresh_interval=None)
        core.track_pairs([_BTC])
        await core.start()
        await _until(lambda: len(probe.tickers) >= 3)

    async def test_explicit_price_interval_overrides_status_poll_interval(
        self, build: Any, probe: Probe
    ) -> None:
        core = build(status_poll_interval=0.02, price_refresh_interval=10.0)
        core.track_pairs([_BTC])
        await core.start()
        await asyncio.sleep(0.3)
        assert len(probe.tickers) <= 1

    async def test_nothing_tracked_and_no_orders_means_no_ticker_calls(
        self, build: Any, probe: Probe
    ) -> None:
        core = build(price_refresh_interval=0.02)
        await core.start()
        await _until(lambda: core.status_dict["trading_rule_initialized"])
        await asyncio.sleep(0.2)
        assert probe.tickers == []

    async def test_only_tracked_pairs_are_polled(self, build: Any, probe: Probe) -> None:
        core = build(price_refresh_interval=0.02)
        core.track_pairs([_BTC])
        await core.start()
        await _until(lambda: len(probe.tickers) >= 3)
        assert set(probe.tickers) == {"XBTUSD"}

    async def test_track_added_while_running_is_picked_up(self, build: Any, probe: Probe) -> None:
        core = build(price_refresh_interval=0.02)
        core.track_pairs([_BTC])
        await core.start()
        await _until(lambda: "XBTUSD" in probe.tickers)
        assert "ETHUSD" not in probe.tickers
        core.track_pairs([_ETH])
        await _until(lambda: "ETHUSD" in probe.tickers)

    async def test_untracked_pair_stops_being_polled(self, build: Any, probe: Probe) -> None:
        core = build(price_refresh_interval=0.02)
        core.track_pairs([_BTC, _ETH])
        await core.start()
        await _until(lambda: "XBTUSD" in probe.tickers and "ETHUSD" in probe.tickers)
        core.untrack_pairs([_BTC])
        await asyncio.sleep(0.1)  # let a tick that was already in flight land
        seen_btc = probe.tickers.count("XBTUSD")
        eth_before = probe.tickers.count("ETHUSD")
        await _until(lambda: probe.tickers.count("ETHUSD") >= eth_before + 3)
        assert probe.tickers.count("XBTUSD") == seen_btc

    async def test_in_flight_order_pair_is_polled_without_tracking(
        self, build: Any, probe: Probe
    ) -> None:
        core = build(price_refresh_interval=0.02)
        await core.start()
        await _until(lambda: _ETH in core.trading_rules)
        client_id = core.sell(_ETH, Decimal("0.5"), OrderType.LIMIT, Decimal("2000"))
        assert client_id in core.in_flight_orders
        assert _ETH not in core.tracked_pairs
        await _until(lambda: "ETHUSD" in probe.tickers)

    async def test_order_books_are_never_refreshed(
        self, build: Any, probe: Probe, rest: base.FakeKrakenRest
    ) -> None:
        core = build(price_refresh_interval=0.02, trading_rules_refresh_interval=0.02)
        core.track_pairs([_BTC, _ETH])
        await core.start()
        await _until(lambda: len(probe.tickers) >= 4 and probe.count("get_asset_pairs") >= 3)
        assert probe.count("get_orderbook") == 0
        assert rest.bodies("depth") == []


class TestTrackedPairsApi:
    async def test_track_untrack_and_frozenset_view(self, build: Any) -> None:
        core = build()
        assert core.tracked_pairs == frozenset()
        assert isinstance(core.tracked_pairs, frozenset)
        assert core.track_pairs([_BTC, _ETH, _BTC]) is None
        assert core.tracked_pairs == frozenset({_BTC, _ETH})
        assert core.untrack_pairs(iter([_BTC, "SOL-USD"])) is None  # unknown pair: ignored
        assert core.tracked_pairs == frozenset({_ETH})

    async def test_returned_view_is_a_snapshot(self, build: Any) -> None:
        core = build()
        core.track_pairs([_BTC])
        view = core.tracked_pairs
        core.track_pairs([_ETH])
        assert view == frozenset({_BTC})


# ---------------------------------------------------------------------------
# 3. Failure isolation
# ---------------------------------------------------------------------------


class TestRefreshFailureIsolation:
    async def test_rules_failure_is_retried_and_recovers(self, build: Any, probe: Probe) -> None:
        probe.fail["get_asset_pairs"] = 2
        core = build(trading_rules_refresh_interval=0.02)
        await core.start()
        await _until(lambda: _BTC in core.trading_rules)
        assert probe.count("get_asset_pairs") >= 3

    async def test_rules_always_failing_does_not_stop_the_other_loops(
        self, build: Any, probe: Probe
    ) -> None:
        probe.fail["get_asset_pairs"] = _FOREVER
        core = build(trading_rules_refresh_interval=0.02, balance_refresh_interval=0.02)
        await core.start()
        await _until(lambda: probe.count("get_asset_pairs") >= 3)
        await _until(lambda: probe.count("get_balances_ex") >= 3)
        status = core.status_dict
        assert status["account_balance"] is True
        assert status["trading_rule_initialized"] is False
        assert status["status_polling_initialized"] is True

    async def test_balance_failure_is_retried_and_recovers(self, build: Any, probe: Probe) -> None:
        probe.fail["get_balances_ex"] = 2
        core = build(balance_refresh_interval=0.02)
        await core.start()
        await _until(lambda: core.status_dict["account_balance"])
        assert probe.count("get_balances_ex") >= 3

    async def test_balance_always_failing_does_not_stop_the_other_loops(
        self, build: Any, probe: Probe
    ) -> None:
        probe.fail["get_balances_ex"] = _FOREVER
        core = build(
            trading_rules_refresh_interval=0.02,
            balance_refresh_interval=0.02,
            price_refresh_interval=0.02,
        )
        core.track_pairs([_BTC])
        await core.start()
        await _until(lambda: probe.count("get_balances_ex") >= 3)
        await _until(lambda: probe.count("get_asset_pairs") >= 3)
        await _until(lambda: len(probe.tickers) >= 3)
        assert core.status_dict["account_balance"] is False
        assert core.status_dict["trading_rule_initialized"] is True

    async def test_price_failure_is_retried_and_recovers(self, build: Any, probe: Probe) -> None:
        probe.fail["get_ticker"] = 2
        core = build(price_refresh_interval=0.02)
        core.track_pairs([_BTC])
        await core.start()
        await _until(lambda: not core.get_price_by_type(_BTC, PriceType.MidPrice).is_nan())
        assert len(probe.tickers) >= 3

    async def test_price_always_failing_does_not_stop_the_other_loops(
        self, build: Any, probe: Probe
    ) -> None:
        probe.fail["get_ticker"] = _FOREVER
        core = build(balance_refresh_interval=0.02, price_refresh_interval=0.02)
        core.track_pairs([_BTC])
        await core.start()
        await _until(lambda: len(probe.tickers) >= 3)
        await _until(lambda: probe.count("get_balances_ex") >= 3)
        assert core.status_dict["account_balance"] is True
        assert core.status_dict["status_polling_initialized"] is True

    async def test_rest_failures_do_not_affect_the_ws_supervisor(
        self, build: Any, probe: Probe, ws: FakeOwnTradesWs
    ) -> None:
        for name in ("get_asset_pairs", "get_balances_ex", "get_ticker"):
            probe.fail[name] = _FOREVER
        core = build(
            trading_rules_refresh_interval=0.02,
            balance_refresh_interval=0.02,
            price_refresh_interval=0.02,
        )
        core.track_pairs([_BTC])
        await core.start()
        await _until(lambda: core.status_dict["user_stream_initialized"])
        assert ws.enters == 1


# ---------------------------------------------------------------------------
# 4. Own-trades WebSocket supervisor
# ---------------------------------------------------------------------------


class TestOwnTradesSupervisor:
    async def test_ws_trade_reaches_on_own_trades(
        self, build: Any, ws: FakeOwnTradesWs, seen: base.Recorder
    ) -> None:
        core = build()
        await core.start()
        await _until(lambda: core.ready)
        client_id = await base._place(core, seen, "sell")
        assert ws.callback == core.on_own_trades
        ws.push(
            base._ws_trades(
                "T1",
                vol="0.2",
                price="27000.0",
                cost="5400.0",
                fee="8.64",
                time="1700000000.5123",
                side="sell",
            )
        )
        (fill,) = seen.of(base.FILLED)
        assert fill.order_id == client_id
        assert fill.amount == Decimal("0.2")
        assert fill.exchange_trade_id == "T1"

    async def test_subscribe_failure_is_retried_with_backoff(
        self, build: Any, ws: FakeOwnTradesWs
    ) -> None:
        ws.fail_enters = 2
        core = build(ws_reconnect_delay=0.1, ws_reconnect_delay_max=1.0)
        await core.start()
        await _until(lambda: core.status_dict["user_stream_initialized"], timeout=5.0)
        assert ws.attempts == 3
        assert ws.enters == 1
        # Two failures back off at least 0.1s each time; allow for timer slack.
        assert ws.attempt_times[2] - ws.attempt_times[0] >= 0.18

    async def test_user_stream_false_while_subscription_keeps_failing(
        self, build: Any, ws: FakeOwnTradesWs
    ) -> None:
        ws.fail_enters = _FOREVER
        core = build()
        assert core.status_dict["user_stream_initialized"] is False
        await core.start()
        await _until(lambda: ws.attempts >= 3)
        assert core.status_dict["user_stream_initialized"] is False
        assert core.ready is False

    async def test_drop_marks_stream_down_then_resubscribes(
        self, build: Any, ws: FakeOwnTradesWs
    ) -> None:
        core = build(ws_reconnect_delay=0.2, ws_reconnect_delay_max=1.0)
        await core.start()
        await _until(lambda: core.status_dict["user_stream_initialized"])
        assert ws.enters == 1
        dropped_at = time.monotonic()
        ws.drop()
        await _until(lambda: core.status_dict["user_stream_initialized"] is False)
        assert core.ready is False
        await _until(lambda: core.status_dict["user_stream_initialized"], timeout=5.0)
        assert ws.enters >= 2
        assert ws.exits >= 1  # the dropped subscription context was exited
        assert ws.attempt_times[-1] - dropped_at >= 0.15  # backoff respected
        assert core.ready is True

    async def test_repeated_drops_keep_resubscribing(self, build: Any, ws: FakeOwnTradesWs) -> None:
        core = build()
        await core.start()
        for round_no in range(1, 4):
            await _until(lambda r=round_no: ws.enters >= r and core.status_dict[_USER_STREAM])
            ws.drop()
        await _until(lambda: ws.enters >= 4 and core.status_dict["user_stream_initialized"])

    async def test_own_trades_ws_disabled_means_no_subscription_and_four_keys(
        self, build: Any, ws: FakeOwnTradesWs
    ) -> None:
        core = build(own_trades_ws=False, balance_refresh_interval=0.02)
        await core.start()
        await _until(lambda: core.ready)
        await asyncio.sleep(0.1)
        assert ws.attempts == 0
        assert ws.callback is None
        assert ws.disconnect_waits == 0
        assert set(core.status_dict) == KEYS_4
        assert "user_stream_initialized" not in core.status_dict
        assert all(core.status_dict.values())


# ---------------------------------------------------------------------------
# 5. Readiness
# ---------------------------------------------------------------------------


class TestReadiness:
    async def test_default_status_dict_has_five_keys(self, build: Any) -> None:
        core = build()
        assert set(core.status_dict) == KEYS_5
        assert not any(core.status_dict.values())
        assert core.ready is False

    async def test_ready_from_start_alone(self, build: Any) -> None:
        core = build()
        assert core.ready is False
        await core.start()
        await _until(lambda: core.ready)
        status = core.status_dict
        assert set(status) == KEYS_5
        assert all(status.values())
        assert core.trading_rules  # filled without any manual update_* call

    async def test_not_ready_until_the_ws_subscription_succeeds(
        self, build: Any, ws: FakeOwnTradesWs
    ) -> None:
        ws.fail_enters = _FOREVER
        core = build(balance_refresh_interval=0.02)
        await core.start()
        await _until(lambda: core.status_dict["account_balance"])
        await _until(lambda: core.status_dict["trading_rule_initialized"])
        await asyncio.sleep(0.1)
        assert core.ready is False

    async def test_status_dict_is_a_fresh_dict_per_call(self, build: Any) -> None:
        core = build()
        await core.start()
        await _until(lambda: core.ready)
        first = core.status_dict
        first["user_stream_initialized"] = False
        first["bogus"] = True
        second = core.status_dict
        assert second is not first
        assert second["user_stream_initialized"] is True
        assert "bogus" not in second
        assert core.ready is True


# ---------------------------------------------------------------------------
# 6. Shutdown
# ---------------------------------------------------------------------------


class TestStop:
    async def test_stop_unsubscribes_and_marks_everything_down(
        self, build: Any, ws: FakeOwnTradesWs
    ) -> None:
        core = build()
        await core.start()
        await _until(lambda: core.ready)
        await asyncio.wait_for(core.stop(), 1.5)
        assert ws.exits == ws.enters == 1
        assert ws.active == 0
        status = core.status_dict
        assert status["user_stream_initialized"] is False
        assert status["status_polling_initialized"] is False
        assert core.ready is False

    async def test_stop_leaves_no_core_task_pending(self, build: Any) -> None:
        baseline = asyncio.all_tasks()
        core = build(
            trading_rules_refresh_interval=0.02,
            balance_refresh_interval=0.02,
            price_refresh_interval=0.02,
        )
        core.track_pairs([_BTC])
        await core.start()
        await _until(lambda: core.ready)
        await asyncio.wait_for(core.stop(), 1.5)
        assert {t for t in asyncio.all_tasks() - baseline if not t.done()} == set()

    async def test_stop_halts_every_refresh_loop(self, build: Any, probe: Probe) -> None:
        core = build(
            trading_rules_refresh_interval=0.02,
            balance_refresh_interval=0.02,
            price_refresh_interval=0.02,
        )
        core.track_pairs([_BTC])
        await core.start()
        await _until(lambda: probe.count("get_balances_ex") >= 2 and len(probe.tickers) >= 2)
        await asyncio.wait_for(core.stop(), 1.5)
        snapshot = {name: probe.count(name) for name in Probe.NAMES}
        await asyncio.sleep(0.15)
        assert {name: probe.count(name) for name in Probe.NAMES} == snapshot

    async def test_stop_is_clean_while_refresh_calls_hang(
        self, build: Any, probe: Probe, ws: FakeOwnTradesWs
    ) -> None:
        probe.hang.update({"get_asset_pairs", "get_balances_ex", "get_ticker"})
        baseline = asyncio.all_tasks()
        core = build(stop_grace=1.0, price_refresh_interval=0.02)
        core.track_pairs([_BTC])
        await core.start()
        await _until(lambda: probe.count("get_asset_pairs") >= 1)
        await _until(lambda: probe.count("get_balances_ex") >= 1)
        await _until(lambda: ws.enters == 1)
        started = time.monotonic()
        await asyncio.wait_for(core.stop(), 1.5)
        assert time.monotonic() - started < 1.0
        assert ws.active == 0
        assert {t for t in asyncio.all_tasks() - baseline if not t.done()} == set()

    async def test_start_then_stop_immediately_is_clean(self, build: Any) -> None:
        baseline = asyncio.all_tasks()
        core = build()
        await core.start()
        await asyncio.wait_for(core.stop(), 1.5)
        assert {t for t in asyncio.all_tasks() - baseline if not t.done()} == set()

    async def test_stop_while_resubscribe_is_backing_off(
        self, build: Any, ws: FakeOwnTradesWs
    ) -> None:
        baseline = asyncio.all_tasks()
        core = build(ws_reconnect_delay=30.0, ws_reconnect_delay_max=60.0)
        await core.start()
        await _until(lambda: core.status_dict["user_stream_initialized"])
        ws.drop()
        await _until(lambda: core.status_dict["user_stream_initialized"] is False)
        await asyncio.wait_for(core.stop(), 1.5)
        assert {t for t in asyncio.all_tasks() - baseline if not t.done()} == set()


# ---------------------------------------------------------------------------
# 7. Protocol
# ---------------------------------------------------------------------------


class TestProtocol:
    def test_protocol_declares_track_and_untrack(self) -> None:
        assert callable(getattr(ConnectorCoreProtocol, "track_pairs", None))
        assert callable(getattr(ConnectorCoreProtocol, "untrack_pairs", None))

    async def test_core_satisfies_protocol(self, build: Any) -> None:
        assert isinstance(build(), ConnectorCoreProtocol)

    async def test_object_without_track_pairs_does_not_satisfy_protocol(self, build: Any) -> None:
        core = build()

        class _Wrapper:
            def __getattr__(self, name: str) -> Any:
                if name in {"track_pairs", "untrack_pairs"}:
                    raise AttributeError(name)
                return getattr(core, name)

        assert not isinstance(_Wrapper(), ConnectorCoreProtocol)


# ---------------------------------------------------------------------------
# 8. Gateway seam
# ---------------------------------------------------------------------------


class _DroppableWs(wsb._FakeWs):
    """Fake socket whose frame iterator raises (a dropped connection) once ``drop()`` is called."""

    def drop(self) -> None:
        self._queue.put_nowait("__drop__")

    async def __anext__(self) -> str:
        frame = await self._queue.get()
        if frame == "__drop__":
            raise ConnectionError("connection dropped")
        return frame


class TestGatewayWaitWsDisconnect:
    async def test_returns_when_the_listen_loop_sees_the_drop(self) -> None:
        gateway = KrakenGateway(api_key="k", secret_key="c2VjcmV0")
        conn = WsConnectorBase(
            url="wss://base.example.com",
            ws_auth=wsb._FakeAuth(),
            decoder=wsb._FakeDecoder(),
            reconnect_delay=30.0,  # the base reconnect must not mask the drop
        )
        gateway._ws_private = conn  # #274: wait_ws_disconnect follows the private connector
        fake = _DroppableWs()
        try:
            await wsb._connect_with_fake_ws(conn, fake)
            waiter = asyncio.create_task(gateway.wait_ws_disconnect())
            await asyncio.sleep(0.05)
            assert not waiter.done()  # connection still up
            fake.drop()
            await asyncio.wait_for(waiter, 1.0)
        finally:
            await conn.disconnect()
