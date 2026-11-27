"""RED acceptance tests for #310 PR 3: the shared Coinbase LegFill hub gate.

Contract pinned here (fixtures only, no network):

* ``_hub(gateway)`` (``market_connector.exchanges.coinbase.mixins.leg_fills``) exposes
  ``observe(cid, cumulative, price, order_id)``: one gate per gateway, callable by both the REST
  poller and (from PR 4) the WS user channel. ``cumulative`` and ``price`` are ``Decimal`` or
  ``str`` (never float); ``order_id`` is the exchange order id of the order instance.
* The gate emits a ``LegFill`` only when ``cumulative`` is > 0 and strictly exceeds the last
  emitted cumulative of that order instance; equal or lower values are dropped, so REST and WS
  reports of one fill de-dupe regardless of arrival order. ``LegFill.cumulative_filled`` is the
  running total (protocol), so the delta of a fill is the difference of consecutive emissions.
  ``fill_id`` is ``None`` and ``timestamp`` is a local-clock float (protocol doc, Coinbase).
* Early buffer (Kraken-style): an observation for an order not registered yet (no
  ``track_leg_placement`` / ``place_order`` for it) emits nothing and is buffered; on
  registration the buffer of that order is replayed in arrival order through the same gate.
* ``_poll_leg`` feeds the same gate; it has no dedupe of its own.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from market_connector.exchanges.coinbase.coinbase_gateway import CoinbaseGateway
from market_connector.exchanges.coinbase.config import CoinbaseConfig
from market_connector.exchanges.coinbase.linked_order_codec import (
    CoinbaseClientOrderIdLinkCodec,
)
from market_connector.exchanges.coinbase.mixins.leg_fills import _hub
from market_connector.transport.response import Response

if TYPE_CHECKING:
    from market_connector.protocols import LegFill

_PAIR = "BTC-USD"
_CODEC = CoinbaseClientOrderIdLinkCodec()
_A = _CODEC.encode(7, 0)
_B = _CODEC.encode(7, 1)
_D = Decimal
_SLOW = 3600.0  # the poll loop polls once on start, then sleeps: later polls are driven by hand


def _xid(cid: str) -> str:
    return f"X-{cid}"


def _row(filled: str, status: str = "OPEN", price: str = "50000") -> dict[str, Any]:
    return {"filled_size": filled, "average_filled_price": price, "status": status}


class _FakeRest:
    """Answers ``place_order`` (exchange id ``X-<cid>``) and scripted ``order_status`` GETs.

    The last scripted row repeats once the script is exhausted; unscripted orders report 0.
    """

    def __init__(self) -> None:
        self.scripts: dict[str, list[dict[str, Any]]] = {}
        self.status_calls: list[str] = []

    async def request(self, endpoint_name: str, **kwargs: Any) -> Response:
        if endpoint_name == "place_order":
            cid = kwargs["data"]["client_order_id"]
            return self._resp({"success": True, "order_id": _xid(cid)})
        if endpoint_name == "order_status":
            oid = (kwargs.get("params") or {})["order_id"]
            self.status_calls.append(oid)
            script = self.scripts.get(oid) or [_row("0", price="0")]
            item = script.pop(0) if len(script) > 1 else script[0]
            order = {
                "order_id": oid,
                "client_order_id": oid.removeprefix("X-"),
                "product_id": _PAIR,
                "side": "BUY",
                **item,
            }
            return self._resp({"order": order})
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

    def cumulative(self, cid: str | None = None) -> list[Decimal]:
        return [f.cumulative_filled for f in self.fills if cid in (None, f.client_order_id)]


async def _wait_until(cond: Any, timeout: float = 1.0) -> bool:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if cond():
            return True
        await asyncio.sleep(0.005)
    return bool(cond())


async def _settle() -> None:
    await asyncio.sleep(0.05)


@pytest.fixture
def rest() -> _FakeRest:
    return _FakeRest()


@pytest.fixture
def gateway(rest: _FakeRest) -> CoinbaseGateway:
    cfg = CoinbaseConfig(
        api_key="k",
        secret_key="raw_hmac_secret",
        sandbox=True,
        leg_fill_poll_interval=_SLOW,
    )
    gw = CoinbaseGateway(cfg)
    gw._rest = rest  # type: ignore[assignment]
    gw._ws = _FakeWs()  # type: ignore[assignment]
    gw._started = True
    return gw


@pytest.fixture
async def sink(gateway: CoinbaseGateway) -> Any:
    s = Sink()
    unsub = gateway.subscribe_leg_fills(s.on_fill)
    yield s
    unsub()
    await _hub(gateway).shutdown()


async def _place(gateway: CoinbaseGateway, cid: str) -> str:
    await gateway.place_order(_PAIR, "limit", "buy", _D("10"), _D("50000"), client_order_id=cid)
    return _xid(cid)


async def _poll_once(gateway: CoinbaseGateway, cid: str) -> None:
    hub = _hub(gateway)
    await hub._poll_leg(cid, hub.legs[cid])


class TestGate:
    async def test_emits_running_total_with_protocol_fields(
        self, gateway: CoinbaseGateway, sink: Sink
    ) -> None:
        xid = await _place(gateway, _A)
        _hub(gateway).observe(_A, _D("0.4"), _D("50010.5"), xid)

        assert len(sink.fills) == 1
        fill = sink.fills[0]
        assert fill.client_order_id == _A
        assert fill.cumulative_filled == _D("0.4")
        assert fill.last_price == _D("50010.5")
        assert fill.fill_id is None
        assert isinstance(fill.timestamp, float)
        assert fill.timestamp > 0

    async def test_equal_and_lower_cumulative_are_dropped(
        self, gateway: CoinbaseGateway, sink: Sink
    ) -> None:
        xid = await _place(gateway, _A)
        hub = _hub(gateway)
        hub.observe(_A, _D("2"), _D("100"), xid)
        hub.observe(_A, _D("2"), _D("101"), xid)
        hub.observe(_A, _D("1.5"), _D("102"), xid)
        hub.observe(_A, _D("3"), _D("103"), xid)

        assert sink.cumulative() == [_D("2"), _D("3")]
        assert sink.fills[-1].last_price == _D("103")

    async def test_zero_cumulative_emits_nothing(
        self, gateway: CoinbaseGateway, sink: Sink
    ) -> None:
        xid = await _place(gateway, _A)
        _hub(gateway).observe(_A, _D("0"), _D("0"), xid)

        assert sink.fills == []

    async def test_two_orders_never_interfere(self, gateway: CoinbaseGateway, sink: Sink) -> None:
        xa = await _place(gateway, _A)
        xb = await _place(gateway, _B)
        hub = _hub(gateway)
        hub.observe(_A, _D("5"), _D("100"), xa)
        hub.observe(_B, _D("1"), _D("200"), xb)
        hub.observe(_B, _D("2"), _D("200"), xb)
        hub.observe(_A, _D("5"), _D("100"), xa)

        assert sink.cumulative(_A) == [_D("5")]
        assert sink.cumulative(_B) == [_D("1"), _D("2")]

    async def test_string_and_decimal_inputs_share_one_gate_without_drift(
        self, gateway: CoinbaseGateway, sink: Sink
    ) -> None:
        xid = await _place(gateway, _A)
        hub = _hub(gateway)
        hub.observe(_A, "0.1", "50000.123456789012345678", xid)
        hub.observe(_A, _D("0.1"), _D("50000.123456789012345678"), xid)  # same fill, Decimal
        hub.observe(_A, "0.30000000000000000001", "1", xid)

        assert sink.cumulative() == [_D("0.1"), _D("0.30000000000000000001")]
        assert sink.fills[0].last_price == _D("50000.123456789012345678")
        assert all(isinstance(f.cumulative_filled, Decimal) for f in sink.fills)
        assert all(isinstance(f.last_price, Decimal) for f in sink.fills)


class TestEarlyBuffer:
    async def test_unregistered_order_is_buffered_then_replayed_in_order(
        self, gateway: CoinbaseGateway, sink: Sink
    ) -> None:
        hub = _hub(gateway)
        hub.observe(_A, "0.4", "100", _xid(_A))
        hub.observe(_A, "0.9", "101", _xid(_A))
        await _settle()
        assert sink.fills == []  # not registered yet: nothing emitted

        await _place(gateway, _A)

        assert await _wait_until(lambda: len(sink.fills) >= 2)
        assert sink.cumulative() == [_D("0.4"), _D("0.9")]
        assert [f.last_price for f in sink.fills] == [_D("100"), _D("101")]

    async def test_replay_goes_through_the_strictly_increasing_gate(
        self, gateway: CoinbaseGateway, sink: Sink
    ) -> None:
        hub = _hub(gateway)
        hub.observe(_A, "0.9", "101", _xid(_A))
        hub.observe(_A, "0.4", "100", _xid(_A))  # late, lower: must be dropped on replay
        hub.observe(_A, "0.9", "101", _xid(_A))  # duplicate

        await _place(gateway, _A)

        assert await _wait_until(lambda: len(sink.fills) >= 1)
        await _settle()
        assert sink.cumulative() == [_D("0.9")]

    async def test_buffer_is_per_order_and_replayed_once(
        self, gateway: CoinbaseGateway, sink: Sink
    ) -> None:
        hub = _hub(gateway)
        hub.observe(_A, "1", "100", _xid(_A))
        hub.observe(_B, "2", "200", _xid(_B))

        await _place(gateway, _A)
        assert await _wait_until(lambda: len(sink.fills) >= 1)
        await _settle()
        assert sink.cumulative(_A) == [_D("1")]
        assert sink.cumulative(_B) == []  # B is still unregistered

        await _place(gateway, _B)
        assert await _wait_until(lambda: len(sink.cumulative(_B)) >= 1)
        await _settle()
        assert sink.cumulative(_A) == [_D("1")]
        assert sink.cumulative(_B) == [_D("2")]

        hub.observe(_A, "1", "100", _xid(_A))  # buffer was consumed, gate remembers
        assert sink.cumulative(_A) == [_D("1")]


class TestPollerUsesSharedGate:
    async def test_observe_then_poll_at_same_cumulative_emits_nothing_extra(
        self, gateway: CoinbaseGateway, rest: _FakeRest, sink: Sink
    ) -> None:
        xid = await _place(gateway, _A)
        rest.scripts[xid] = [_row("1.5", price="50000")]
        _hub(gateway).observe(_A, "1.5", "50000", xid)  # WS-style report first
        assert sink.cumulative() == [_D("1.5")]

        calls_before = len(rest.status_calls)
        await _poll_once(gateway, _A)
        await _settle()

        assert len(rest.status_calls) > calls_before  # the poll did see 1.5
        assert sink.cumulative() == [_D("1.5")]

    async def test_poll_then_observe_at_same_cumulative_emits_nothing_extra(
        self, gateway: CoinbaseGateway, rest: _FakeRest, sink: Sink
    ) -> None:
        rest.scripts[_xid(_A)] = [_row("1.5", price="50000")]
        xid = await _place(gateway, _A)
        assert await _wait_until(lambda: len(sink.fills) >= 1)  # the loop's first poll
        assert sink.cumulative() == [_D("1.5")]

        _hub(gateway).observe(_A, "1.5", "50000", xid)

        assert sink.cumulative() == [_D("1.5")]

    async def test_poll_continues_above_a_ws_observed_cumulative(
        self, gateway: CoinbaseGateway, rest: _FakeRest, sink: Sink
    ) -> None:
        xid = await _place(gateway, _A)
        _hub(gateway).observe(_A, "1", "100", xid)
        rest.scripts[xid] = [_row("2", price="101")]
        await _poll_once(gateway, _A)

        assert sink.cumulative() == [_D("1"), _D("2")]
        assert sink.fills[-1].last_price == _D("101")


class TestInterleaving:
    async def test_rest_ws_rest_ws_emits_exactly_two_fills(
        self, gateway: CoinbaseGateway, rest: _FakeRest, sink: Sink
    ) -> None:
        rest.scripts[_xid(_A)] = [_row("1"), _row("2")]
        xid = await _place(gateway, _A)
        hub = _hub(gateway)

        assert await _wait_until(lambda: len(sink.fills) >= 1)  # REST cumulative 1
        hub.observe(_A, "3", "100", xid)  # WS cumulative 3
        await _poll_once(gateway, _A)  # REST cumulative 2 (stale)
        hub.observe(_A, "3", "100", xid)  # WS cumulative 3 again

        c = sink.cumulative()
        assert c == [_D("1"), _D("3")]
        deltas = [cur - prev for prev, cur in zip([_D(0), *c[:-1]], c, strict=True)]
        assert deltas == [_D("1"), _D("2")]
