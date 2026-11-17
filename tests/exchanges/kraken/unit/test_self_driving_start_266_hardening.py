"""Review hardening of the self-driving start (#266): stop bound, retry, validation, cleanup."""

# ruff: noqa: F811  (fixtures are imported from the 266 module and used by name)

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from market_connector.exchanges.kraken.mixins.subscriptions import SubscriptionsMixin
from tests.exchanges.kraken.unit.test_self_driving_start_266 import (  # noqa: F401
    FakeOwnTradesWs,
    Probe,
    _until,
    build,
    bus,
    gateway,
    probe,
    rest,
    seen,
    ws,
)

_GRACE = 0.2
_EPS = 0.15  # short bounded abandon wait + scheduling slack


# ---------------------------------------------------------------------------
# F1: stop() bounded by one stop_grace
# ---------------------------------------------------------------------------


class TestStopBound:
    async def test_stop_returns_within_one_grace_despite_stuck_tasks(
        self,
        build: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        core = build(stop_grace=_GRACE)
        release = asyncio.Event()
        ignored = False

        async def stubborn_refresh() -> None:
            nonlocal ignored
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if ignored:
                    raise
                ignored = True
                await asyncio.Event().wait()

        async def stuck_order() -> None:
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue

        monkeypatch.setattr(core, "update_trading_rules", stubborn_refresh)
        await core.start()
        core._track(asyncio.get_running_loop().create_task(stuck_order()))
        await asyncio.sleep(0.05)
        loops = list(core._loop_tasks)

        started = time.monotonic()
        stopper = asyncio.get_running_loop().create_task(core.stop())
        done, _ = await asyncio.wait({stopper}, timeout=3.0)  # wait_for would hang on a stuck stop
        elapsed = time.monotonic() - started
        release.set()
        await asyncio.wait({stopper}, timeout=3.0)

        assert done, "stop() did not return"
        assert elapsed < _GRACE + _EPS
        assert all(t.done() for t in loops)


# ---------------------------------------------------------------------------
# F4: failed refresh retried sooner
# ---------------------------------------------------------------------------


class TestRefreshRetry:
    async def test_failed_rules_refresh_retries_after_retry_delay(
        self,
        build: Any,
        probe: Probe,
    ) -> None:
        probe.fail["get_asset_pairs"] = 1
        core = build(trading_rules_refresh_interval=10.0, refresh_retry_delay=0.02)
        await core.start()
        await _until(lambda: core.status_dict["trading_rule_initialized"], timeout=1.0)
        assert probe.count("get_asset_pairs") == 2

    async def test_retry_delay_is_capped_by_interval(
        self,
        build: Any,
        probe: Probe,
    ) -> None:
        probe.fail["get_asset_pairs"] = 3
        core = build(trading_rules_refresh_interval=0.02, refresh_retry_delay=100.0)
        await core.start()
        await _until(lambda: probe.count("get_asset_pairs") >= 4, timeout=1.0)


# ---------------------------------------------------------------------------
# F6 / F7: argument validation
# ---------------------------------------------------------------------------


class TestArgumentValidation:
    @pytest.mark.parametrize("method", ["track_pairs", "untrack_pairs"])
    def test_bare_string_pair_is_rejected(self, build: Any, method: str) -> None:
        core = build()
        with pytest.raises(TypeError):
            getattr(core, method)("BTC-USD")
        assert core.tracked_pairs == frozenset()

    @pytest.mark.parametrize(
        "option",
        [
            "status_poll_interval",
            "trading_rules_refresh_interval",
            "balance_refresh_interval",
            "price_refresh_interval",
            "ws_reconnect_delay",
            "ws_reconnect_delay_max",
            "refresh_retry_delay",
        ],
    )
    @pytest.mark.parametrize("value", [0, -1.0])
    def test_non_positive_interval_is_rejected(
        self,
        build: Any,
        option: str,
        value: float,
    ) -> None:
        with pytest.raises(ValueError, match=option):
            build(**{option: value})


# ---------------------------------------------------------------------------
# F8: poll loop survives unexpected errors
# ---------------------------------------------------------------------------


class TestPollLoopResilience:
    async def test_unexpected_poll_error_does_not_kill_the_loop(
        self,
        build: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        core = build(status_poll_interval=0.01)
        calls = 0

        async def flaky() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("boom")

        monkeypatch.setattr(core, "_poll_once", flaky)
        await core.start()
        await _until(lambda: calls >= 3, timeout=1.0)
        assert core.status_dict["status_polling_initialized"] is True

    async def test_cancellation_still_stops_the_poll_loop(self, build: Any) -> None:
        core = build(status_poll_interval=0.01)
        await core.start()
        poll = core._poll_task
        await core.stop()
        assert poll is not None
        assert poll.done()
        assert core.status_dict["status_polling_initialized"] is False


# ---------------------------------------------------------------------------
# F5: subscribe route cleanup when the subscribe frame cannot be sent
# ---------------------------------------------------------------------------


class _RouteWs:
    def __init__(self) -> None:
        self.routes: set[tuple[str, str | None]] = set()

    def subscribe(self, channel: str, pair: str | None, handler: Any) -> None:
        self.routes.add((channel, pair))

    def unsubscribe(self, channel: str, pair: str | None) -> None:
        self.routes.discard((channel, pair))

    async def send(self, message: dict[str, Any]) -> None:
        raise ConnectionError("send failed")


class _Host(SubscriptionsMixin):
    ready = True

    def __init__(self) -> None:
        self._ws = _RouteWs()
        self._ws_private = self._ws  # #274: private subscribes run on the private connector


class TestSubscribeRouteCleanup:
    @pytest.mark.parametrize(
        ("method", "args"),
        [
            ("subscribe_orderbook", ("XBT/USD", print)),
            ("subscribe_trades", ("XBT/USD", print)),
            ("subscribe_own_trades", (print,)),
            ("subscribe_open_orders", (print,)),
        ],
    )
    async def test_send_failure_leaves_no_route(self, method: str, args: tuple[Any, ...]) -> None:
        host = _Host()
        ctx = await getattr(host, method)(*args)
        with pytest.raises(ConnectionError):
            async with ctx:
                pytest.fail("body must not run")
        assert host._ws.routes == set()
