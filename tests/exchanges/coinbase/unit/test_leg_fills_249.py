"""RED acceptance tests for #249 (Coinbase part): per-leg cumulative ``LegFill`` via REST polling.

Coinbase has no authenticated WS user channel, so the gateway polls. Contract pinned here:

* ``CoinbaseGateway`` satisfies ``SupportsLegFills`` (``subscribe_leg_fills(callback)`` is sync,
  returns an idempotent unsubscribe; callback sync or async; callback errors never propagate).
* New config field ``CoinbaseConfig.leg_fill_poll_interval: float = 1.0`` (seconds).
* Tracked legs: orders placed via ``CoinbaseGateway.place_order`` whose ``client_order_id``
  decodes with ``CoinbaseClientOrderIdLinkCodec``. Foreign ids are never polled nor emitted.
* While there is a subscriber and a non-terminal tracked leg, a background task polls each leg
  with the ``order_status`` endpoint (``params={"order_id": <exchange id>}``, response body
  ``{"order": {...}}``) using the exchange id returned by ``place_order``.
* Emits ``LegFill(cid, Decimal(filled_size), Decimal(average_filled_price), float ts, None)``
  only when ``filled_size`` strictly exceeds the last emitted value (and is > 0).
* After a poll shows FILLED / CANCELLED / EXPIRED / FAILED (final increase emitted first), the
  leg is never polled again. Poll errors are logged and polling continues.
* No subscribers -> no polling; last unsubscribe stops polling; ``stop()`` cancels the task.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import httpx
import pytest

from market_connector.exchanges.coinbase.coinbase_gateway import CoinbaseGateway
from market_connector.exchanges.coinbase.config import CoinbaseConfig
from market_connector.exchanges.coinbase.linked_order_codec import (
    CoinbaseClientOrderIdLinkCodec,
)
from market_connector.protocols import LegFill, SupportsLegFills
from market_connector.transport.response import Response

_PAIR = "BTC-USD"
_CODEC = CoinbaseClientOrderIdLinkCodec()
_A = _CODEC.encode(5, 0)
_B = _CODEC.encode(5, 1)
_FOREIGN = "coinbase-0a1b2c3d"
_D = Decimal
_INTERVAL = 0.01


def _row(filled: str, status: str = "OPEN", price: str = "50000") -> dict[str, Any]:
    return {"filled_size": filled, "average_filled_price": price, "status": status}


class _FakeRest:
    """Answers ``place_order`` (exchange id ``X-<cid>``) and scripted ``order_status`` GETs.

    ``scripts[exchange_id]`` is a list of rows (dict) or exceptions, consumed one per poll;
    the last entry repeats once the script is exhausted.
    """

    def __init__(self) -> None:
        self.scripts: dict[str, list[Any]] = {}
        self.status_calls: list[str] = []
        self.other_calls: list[str] = []

    def calls_for(self, exchange_id: str) -> int:
        return self.status_calls.count(exchange_id)

    async def request(self, endpoint_name: str, **kwargs: Any) -> Response:
        if endpoint_name == "place_order":
            cid = kwargs["data"]["client_order_id"]
            return self._resp({"success": True, "order_id": f"X-{cid}"})
        if endpoint_name == "order_status":
            oid = (kwargs.get("params") or {})["order_id"]
            self.status_calls.append(oid)
            script = self.scripts[oid]
            item = script.pop(0) if len(script) > 1 else script[0]
            if isinstance(item, Exception):
                raise item
            order = {
                "order_id": oid,
                "client_order_id": oid.removeprefix("X-"),
                "product_id": _PAIR,
                "side": "BUY",
                **item,
            }
            return self._resp({"order": order})
        self.other_calls.append(endpoint_name)
        raise AssertionError(f"unexpected endpoint {endpoint_name}")

    async def close(self) -> None:
        return None

    @staticmethod
    def _resp(body: dict[str, Any]) -> Response:
        return Response(raw=body, status_code=200, headers=httpx.Headers(), _endpoint="x")


class _FakeWs:
    async def disconnect(self) -> None:
        return None


class Sink:
    def __init__(self) -> None:
        self.fills: list[LegFill] = []

    def on_fill(self, fill: LegFill) -> None:
        self.fills.append(fill)

    async def on_fill_async(self, fill: LegFill) -> None:
        self.fills.append(fill)

    @property
    def cumulative(self) -> list[Decimal]:
        return [f.cumulative_filled for f in self.fills]


async def _wait_until(cond: Any, timeout: float = 1.0) -> bool:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if cond():
            return True
        await asyncio.sleep(0.005)
    return bool(cond())


async def _quiet(ticks: int = 6) -> None:
    await asyncio.sleep(_INTERVAL * ticks)


@pytest.fixture
def rest() -> _FakeRest:
    return _FakeRest()


@pytest.fixture
def gateway(rest: _FakeRest) -> CoinbaseGateway:
    cfg = CoinbaseConfig(
        api_key="k",
        secret_key="raw_hmac_secret",
        sandbox=True,
        leg_fill_poll_interval=_INTERVAL,
    )
    gw = CoinbaseGateway(cfg)
    gw._rest = rest  # type: ignore[assignment]
    gw._ws = _FakeWs()  # type: ignore[assignment]
    gw._started = True
    return gw


async def _place(gateway: CoinbaseGateway, cid: str) -> str:
    await gateway.place_order(_PAIR, "limit", "buy", _D("1.0"), _D("50000"), client_order_id=cid)
    return f"X-{cid}"


def _script(rest: _FakeRest, xid: str, *rows: Any) -> None:
    rest.scripts[xid] = list(rows)


def test_config_default_interval() -> None:
    cfg = CoinbaseConfig(api_key="k", secret_key="s", sandbox=True)
    assert cfg.leg_fill_poll_interval == 1.0


def test_gateway_is_supports_leg_fills(gateway: CoinbaseGateway) -> None:
    assert isinstance(gateway, SupportsLegFills)


class TestEmission:
    async def test_full_fill_emits_one_legfill(
        self, gateway: CoinbaseGateway, rest: _FakeRest
    ) -> None:
        xid = await _place(gateway, _A)
        _script(rest, xid, _row("1.0", "FILLED", "50010.5"))
        sink = Sink()
        unsub = gateway.subscribe_leg_fills(sink.on_fill)
        try:
            assert await _wait_until(lambda: len(sink.fills) >= 1)
            await _quiet()
        finally:
            unsub()

        assert len(sink.fills) == 1
        fill = sink.fills[0]
        assert fill.client_order_id == _A
        assert fill.cumulative_filled == _D("1.0")
        assert fill.last_price == _D("50010.5")
        assert fill.fill_id is None
        assert isinstance(fill.timestamp, float)
        assert fill.timestamp > 0

    async def test_partial_fills_accumulate(
        self, gateway: CoinbaseGateway, rest: _FakeRest
    ) -> None:
        xid = await _place(gateway, _A)
        _script(
            rest,
            xid,
            _row("0.3", price="50001"),
            _row("0.7", price="50002"),
            _row("1.0", "FILLED", "50003"),
        )
        sink = Sink()
        unsub = gateway.subscribe_leg_fills(sink.on_fill)
        try:
            assert await _wait_until(lambda: len(sink.fills) >= 3)
            await _quiet()
        finally:
            unsub()

        assert sink.cumulative == [_D("0.3"), _D("0.7"), _D("1.0")]
        assert [f.last_price for f in sink.fills] == [_D("50001"), _D("50002"), _D("50003")]

    async def test_zero_filled_emits_nothing(
        self, gateway: CoinbaseGateway, rest: _FakeRest
    ) -> None:
        xid = await _place(gateway, _A)
        _script(rest, xid, _row("0", price="0"))
        sink = Sink()
        unsub = gateway.subscribe_leg_fills(sink.on_fill)
        try:
            assert await _wait_until(lambda: rest.calls_for(xid) >= 3)
        finally:
            unsub()

        assert sink.fills == []

    async def test_final_increase_emitted_before_terminal_stop(
        self, gateway: CoinbaseGateway, rest: _FakeRest
    ) -> None:
        xid = await _place(gateway, _A)
        _script(rest, xid, _row("0.2"), _row("0.5", "CANCELLED"))
        sink = Sink()
        unsub = gateway.subscribe_leg_fills(sink.on_fill)
        try:
            assert await _wait_until(lambda: len(sink.fills) >= 2)
            await _quiet()
        finally:
            unsub()

        assert sink.cumulative == [_D("0.2"), _D("0.5")]

    async def test_each_leg_polled_by_its_own_exchange_id(
        self, gateway: CoinbaseGateway, rest: _FakeRest
    ) -> None:
        xa = await _place(gateway, _A)
        xb = await _place(gateway, _B)
        _script(rest, xa, _row("0.4", "FILLED"))
        _script(rest, xb, _row("0.9", "FILLED"))
        sink = Sink()
        unsub = gateway.subscribe_leg_fills(sink.on_fill)
        try:
            assert await _wait_until(lambda: len(sink.fills) >= 2)
            await _quiet()
        finally:
            unsub()

        by_cid = {f.client_order_id: f.cumulative_filled for f in sink.fills}
        assert by_cid == {_A: _D("0.4"), _B: _D("0.9")}
        assert len(sink.fills) == 2

    async def test_leg_placed_after_subscribe_is_tracked(
        self, gateway: CoinbaseGateway, rest: _FakeRest
    ) -> None:
        sink = Sink()
        unsub = gateway.subscribe_leg_fills(sink.on_fill)
        try:
            await _quiet(3)
            xid = await _place(gateway, _A)
            _script(rest, xid, _row("1.0", "FILLED"))
            assert await _wait_until(lambda: len(sink.fills) >= 1)
        finally:
            unsub()

        assert sink.cumulative == [_D("1.0")]

    async def test_foreign_client_order_id_not_tracked(
        self, gateway: CoinbaseGateway, rest: _FakeRest
    ) -> None:
        await _place(gateway, _FOREIGN)
        _script(rest, f"X-{_FOREIGN}", _row("1.0", "FILLED"))
        sink = Sink()
        unsub = gateway.subscribe_leg_fills(sink.on_fill)
        try:
            await _quiet(8)
        finally:
            unsub()

        assert sink.fills == []
        assert rest.status_calls == []


class TestNoDuplicateEmission:
    async def test_unchanged_filled_size_emits_once(
        self, gateway: CoinbaseGateway, rest: _FakeRest
    ) -> None:
        xid = await _place(gateway, _A)
        _script(rest, xid, _row("0.3"))
        sink = Sink()
        unsub = gateway.subscribe_leg_fills(sink.on_fill)
        try:
            assert await _wait_until(lambda: rest.calls_for(xid) >= 5)
        finally:
            unsub()

        assert sink.cumulative == [_D("0.3")]

    async def test_out_of_order_smaller_value_emits_nothing(
        self, gateway: CoinbaseGateway, rest: _FakeRest
    ) -> None:
        xid = await _place(gateway, _A)
        _script(rest, xid, _row("0.5"), _row("0.3"), _row("0.5"))
        sink = Sink()
        unsub = gateway.subscribe_leg_fills(sink.on_fill)
        try:
            assert await _wait_until(lambda: rest.calls_for(xid) >= 5)
        finally:
            unsub()

        assert sink.cumulative == [_D("0.5")]


class TestTerminal:
    @pytest.mark.parametrize("status", ["FILLED", "CANCELLED", "EXPIRED", "FAILED"])
    async def test_terminal_leg_is_not_polled_again(
        self, gateway: CoinbaseGateway, rest: _FakeRest, status: str
    ) -> None:
        xid = await _place(gateway, _A)
        _script(rest, xid, _row("0.5", status))
        sink = Sink()
        unsub = gateway.subscribe_leg_fills(sink.on_fill)
        try:
            assert await _wait_until(lambda: len(sink.fills) >= 1)
            await _quiet()
            settled = rest.calls_for(xid)
            await _quiet(8)
        finally:
            unsub()

        assert settled >= 1
        assert rest.calls_for(xid) == settled
        assert sink.cumulative == [_D("0.5")]

    async def test_terminal_leg_with_no_fill_stops_polling(
        self, gateway: CoinbaseGateway, rest: _FakeRest
    ) -> None:
        xid = await _place(gateway, _A)
        _script(rest, xid, _row("0", "CANCELLED", "0"))
        sink = Sink()
        unsub = gateway.subscribe_leg_fills(sink.on_fill)
        try:
            assert await _wait_until(lambda: rest.calls_for(xid) >= 1)
            await _quiet()
            settled = rest.calls_for(xid)
            await _quiet(8)
        finally:
            unsub()

        assert rest.calls_for(xid) == settled
        assert sink.fills == []

    async def test_only_terminal_leg_stops_other_keeps_polling(
        self, gateway: CoinbaseGateway, rest: _FakeRest
    ) -> None:
        xa = await _place(gateway, _A)
        xb = await _place(gateway, _B)
        _script(rest, xa, _row("1.0", "FILLED"))
        _script(rest, xb, _row("0.1"), _row("0.2"), _row("0.3"))
        sink = Sink()
        unsub = gateway.subscribe_leg_fills(sink.on_fill)
        try:
            assert await _wait_until(lambda: rest.calls_for(xb) >= 4)
            a_calls = rest.calls_for(xa)
            b_before = rest.calls_for(xb)
            await _quiet(6)
        finally:
            unsub()

        assert rest.calls_for(xa) == a_calls
        assert rest.calls_for(xb) > b_before


class TestErrors:
    async def test_poll_error_is_survived_and_later_poll_emits(
        self, gateway: CoinbaseGateway, rest: _FakeRest
    ) -> None:
        xid = await _place(gateway, _A)
        _script(
            rest,
            xid,
            RuntimeError("boom"),
            httpx.ConnectError("down"),
            _row("0.6", "FILLED"),
        )
        sink = Sink()
        unsub = gateway.subscribe_leg_fills(sink.on_fill)
        try:
            assert await _wait_until(lambda: len(sink.fills) >= 1)
        finally:
            unsub()

        assert sink.cumulative == [_D("0.6")]
        assert rest.calls_for(xid) >= 3

    async def test_error_after_partial_does_not_reemit(
        self, gateway: CoinbaseGateway, rest: _FakeRest
    ) -> None:
        xid = await _place(gateway, _A)
        _script(rest, xid, _row("0.4"), RuntimeError("boom"), _row("0.4"), _row("1.0", "FILLED"))
        sink = Sink()
        unsub = gateway.subscribe_leg_fills(sink.on_fill)
        try:
            assert await _wait_until(lambda: len(sink.fills) >= 2)
            await _quiet()
        finally:
            unsub()

        assert sink.cumulative == [_D("0.4"), _D("1.0")]

    async def test_callback_error_is_swallowed(
        self, gateway: CoinbaseGateway, rest: _FakeRest
    ) -> None:
        xid = await _place(gateway, _A)
        _script(rest, xid, _row("0.5"), _row("1.0", "FILLED"))
        good = Sink()

        def bad(_fill: LegFill) -> None:
            raise RuntimeError("callback boom")

        unsub_bad = gateway.subscribe_leg_fills(bad)
        unsub_good = gateway.subscribe_leg_fills(good.on_fill)
        try:
            assert await _wait_until(lambda: len(good.fills) >= 2)
        finally:
            unsub_bad()
            unsub_good()

        assert good.cumulative == [_D("0.5"), _D("1.0")]


class TestCallbacks:
    async def test_sync_and_async_callbacks_both_receive(
        self, gateway: CoinbaseGateway, rest: _FakeRest
    ) -> None:
        xid = await _place(gateway, _A)
        _script(rest, xid, _row("1.0", "FILLED"))
        sync_sink, async_sink = Sink(), Sink()
        u1 = gateway.subscribe_leg_fills(sync_sink.on_fill)
        u2 = gateway.subscribe_leg_fills(async_sink.on_fill_async)
        try:
            assert await _wait_until(lambda: sync_sink.fills and async_sink.fills)  # type: ignore[arg-type]
        finally:
            u1()
            u2()

        assert sync_sink.cumulative == [_D("1.0")]
        assert async_sink.cumulative == [_D("1.0")]


class TestLifecycle:
    async def test_no_subscribers_no_polling(
        self, gateway: CoinbaseGateway, rest: _FakeRest
    ) -> None:
        xid = await _place(gateway, _A)
        _script(rest, xid, _row("0.5"))
        await _quiet(10)

        assert rest.status_calls == []
        assert rest.other_calls == []

    async def test_unsubscribe_last_subscriber_stops_polling(
        self, gateway: CoinbaseGateway, rest: _FakeRest
    ) -> None:
        xid = await _place(gateway, _A)
        _script(rest, xid, _row("0.5"))
        sink = Sink()
        unsub = gateway.subscribe_leg_fills(sink.on_fill)
        assert await _wait_until(lambda: rest.calls_for(xid) >= 2)
        unsub()
        await _quiet(2)
        settled = rest.calls_for(xid)
        await _quiet(8)

        assert rest.calls_for(xid) == settled

    async def test_polling_continues_while_another_subscriber_remains(
        self, gateway: CoinbaseGateway, rest: _FakeRest
    ) -> None:
        xid = await _place(gateway, _A)
        _script(rest, xid, _row("0.5"))
        s1, s2 = Sink(), Sink()
        u1 = gateway.subscribe_leg_fills(s1.on_fill)
        u2 = gateway.subscribe_leg_fills(s2.on_fill)
        try:
            assert await _wait_until(lambda: rest.calls_for(xid) >= 2)
            u1()
            await _quiet(2)
            before = rest.calls_for(xid)
            assert await _wait_until(lambda: rest.calls_for(xid) > before)
        finally:
            u2()

    async def test_unsubscribe_is_idempotent(
        self, gateway: CoinbaseGateway, rest: _FakeRest
    ) -> None:
        sink = Sink()
        unsub = gateway.subscribe_leg_fills(sink.on_fill)
        unsub()
        unsub()

    async def test_resubscribe_after_full_unsubscribe_resumes(
        self, gateway: CoinbaseGateway, rest: _FakeRest
    ) -> None:
        xid = await _place(gateway, _A)
        _script(rest, xid, _row("0.5"), _row("0.5"), _row("1.0", "FILLED"))
        first = Sink()
        unsub = gateway.subscribe_leg_fills(first.on_fill)
        assert await _wait_until(lambda: len(first.fills) >= 1)
        unsub()
        await _quiet(3)
        second = Sink()
        unsub2 = gateway.subscribe_leg_fills(second.on_fill)
        try:
            # A late subscriber is first replayed the latest known total (#323), then live.
            assert await _wait_until(lambda: second.cumulative[-1:] == [_D("1.0")])
            await _quiet(3)
        finally:
            unsub2()

        seen = second.cumulative
        assert seen[0] == first.cumulative[-1]  # the replay of what the first subscriber last saw
        assert all(a < b for a, b in zip(seen, seen[1:], strict=False))  # no duplicates
        assert seen[-1] == _D("1.0")

    async def test_stop_cancels_poll_task_cleanly(
        self, gateway: CoinbaseGateway, rest: _FakeRest
    ) -> None:
        xid = await _place(gateway, _A)
        _script(rest, xid, _row("0.5"))
        before = asyncio.all_tasks()
        sink = Sink()
        gateway.subscribe_leg_fills(sink.on_fill)
        assert await _wait_until(lambda: rest.calls_for(xid) >= 2)
        assert asyncio.all_tasks() - before

        await gateway.stop()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        leftover = {t for t in asyncio.all_tasks() - before if not t.done()}
        assert leftover == set()
        settled = rest.calls_for(xid)
        await _quiet(8)
        assert rest.calls_for(xid) == settled
