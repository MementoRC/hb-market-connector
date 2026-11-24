"""Regression tests for the PR #311 review fixes (Coinbase leg-fill polling)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import ValidationError

from market_connector.exchanges.coinbase.coinbase_gateway import CoinbaseGateway
from market_connector.exchanges.coinbase.config import CoinbaseConfig
from market_connector.exchanges.coinbase.mixins import leg_fills
from market_connector.protocols import LegFill

from .test_leg_fills_249 import (
    _A,
    _D,
    Sink,
    _FakeRest,
    _FakeWs,
    _place,
    _quiet,
    _row,
    _script,
    _wait_until,
)


@pytest.fixture
def rest() -> _FakeRest:
    return _FakeRest()


@pytest.fixture
def gateway(rest: _FakeRest) -> CoinbaseGateway:
    cfg = CoinbaseConfig(
        api_key="k", secret_key="raw_hmac_secret", sandbox=True, leg_fill_poll_interval=0.01
    )
    gw = CoinbaseGateway(cfg)
    gw._rest = rest  # type: ignore[assignment]
    gw._ws = _FakeWs()  # type: ignore[assignment]
    gw._started = True
    return gw


@pytest.mark.parametrize("bad", [0, -1.0])
def test_nonpositive_poll_interval_rejected(bad: float) -> None:
    with pytest.raises(ValidationError):
        CoinbaseConfig(api_key="k", secret_key="s", leg_fill_poll_interval=bad)


@pytest.mark.parametrize("bad", [0, -1])
def test_nonpositive_max_tracked_rejected(bad: int) -> None:
    with pytest.raises(ValidationError):
        CoinbaseConfig(api_key="k", secret_key="s", leg_fill_max_tracked=bad)


def test_max_tracked_defaults_to_4096() -> None:
    assert CoinbaseConfig(api_key="k", secret_key="s").leg_fill_max_tracked == 4096


def test_tracking_cap_is_configurable_and_evicts_oldest() -> None:
    cfg = CoinbaseConfig(api_key="k", secret_key="s", leg_fill_max_tracked=2)
    hub = leg_fills._LegFillHub(type("_Gw", (), {"_config": cfg})())
    for i in (1, 2, 3):
        hub.track(f"c{i}", f"e{i}")
    assert list(hub.legs) == ["c2", "c3"]


async def test_malformed_price_does_not_lose_fill(gateway: Any, rest: _FakeRest) -> None:
    xid = await _place(gateway, _A)
    _script(rest, xid, _row("0.5", price="not-a-number"), _row("0.5", price="50000"))
    sink = Sink()
    unsub = gateway.subscribe_leg_fills(sink.on_fill)
    try:
        assert await _wait_until(lambda: len(sink.fills) >= 1)
    finally:
        unsub()
    assert sink.cumulative == [_D("0.5")]


async def test_callable_object_with_async_call_is_ordered(gateway: Any, rest: _FakeRest) -> None:
    seen: list[Any] = []

    class _Cb:
        async def __call__(self, fill: LegFill) -> None:
            await asyncio.sleep(0.02 if fill.cumulative_filled == _D("0.3") else 0)
            seen.append(fill.cumulative_filled)

    xid = await _place(gateway, _A)
    _script(rest, xid, _row("0.3"), _row("0.7"), _row("1.0", "FILLED"))
    unsub = gateway.subscribe_leg_fills(_Cb())
    try:
        assert await _wait_until(lambda: len(seen) >= 3)
    finally:
        unsub()
    assert seen == [_D("0.3"), _D("0.7"), _D("1.0")]


async def test_polling_resumes_after_restart_and_not_during_stop(
    gateway: Any, rest: _FakeRest
) -> None:
    xid = await _place(gateway, _A)
    _script(rest, xid, _row("0.5"), _row("1.0", "FILLED"))
    sink = Sink()
    unsub = gateway.subscribe_leg_fills(sink.on_fill)
    try:
        await gateway.stop()
        before = len(rest.status_calls)
        # A placement resolving after stop must not restart polling.
        leg_fills.track_leg_placement(gateway, _A, xid)
        await _quiet()
        assert len(rest.status_calls) == before
        gateway._started = False
        gateway._rest = rest
        leg_fills.resume_leg_fills(gateway)
        assert await _wait_until(lambda: len(sink.fills) >= 1)
    finally:
        unsub()


async def test_replacing_same_leg_keeps_cumulative(gateway: Any, rest: _FakeRest) -> None:
    xid = await _place(gateway, _A)
    _script(rest, xid, _row("0.5"))
    sink = Sink()
    unsub = gateway.subscribe_leg_fills(sink.on_fill)
    try:
        assert await _wait_until(lambda: len(sink.fills) >= 1)
        leg_fills.track_leg_placement(gateway, _A, xid)
        await _quiet()
    finally:
        unsub()
    assert sink.cumulative == [_D("0.5")]


def test_async_queue_coalesces_superseded_fills() -> None:
    sub = leg_fills._Subscriber(Sink().on_fill_async)
    for i in range(leg_fills._MAX_QUEUED + 50):
        sub.queue.append(LegFill(_A, _D(i + 1), _D(1), 1.0, None))
        leg_fills._LegFillHub._bound_queue(sub)
    assert len(sub.queue) <= leg_fills._MAX_QUEUED
    assert sub.queue[-1].cumulative_filled == _D(leg_fills._MAX_QUEUED + 50)
