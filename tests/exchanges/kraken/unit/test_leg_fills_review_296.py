# ruff: noqa: F811
"""RED tests for the automated review findings on PR #296 (Kraken ``LegFill`` push stream).

Gateway-placed legs only (``place_order(cl_ord_id=...)``); trades arrive through
``core.on_own_trades``. See ``test_leg_fills_249.py`` for the base contract.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest

from tests.exchanges.kraken.unit import test_kraken_connector_core_211 as base
from tests.exchanges.kraken.unit.test_leg_fills_249 import Sink, _settle, core  # noqa: F401

if TYPE_CHECKING:
    from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway

bus = base.bus
seen = base.seen

_D = Decimal
_TIME_WS = "1700000000.5"


class GatedRest(base.FakeKrakenRest):
    """Fake REST whose AddOrder txid is unique per call and can be held back per cl_ord_id."""

    def __init__(self) -> None:
        super().__init__()
        self.gates: dict[str, asyncio.Event] = {}
        self.in_flight: set[str] = set()

    async def request(
        self, endpoint_name: str, data: Any = None, params: Any = None, **kw: Any
    ) -> Any:
        if endpoint_name == "add_order":
            cl = (data or {}).get("cl_ord_id", "")
            self.in_flight.add(cl)
            gate = self.gates.get(cl)
            if gate is not None:
                await gate.wait()
        return await super().request(endpoint_name, data, params, **kw)

    def _reply(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        reply = super()._reply(endpoint, body)
        if endpoint == "add_order" and not self.add_order_errors:
            reply["result"]["txid"] = [f"TX{self._n}-{body.get('cl_ord_id', '')}"]
        return reply


@pytest.fixture
def rest() -> GatedRest:
    return GatedRest()


@pytest.fixture
def gateway(rest: GatedRest) -> KrakenGateway:
    gw = base.KrakenGateway(api_key="k", secret_key="c2VjcmV0")
    gw._rest = rest  # type: ignore[assignment]
    gw._started = True
    return gw


async def _place(gateway: KrakenGateway, cl_ord_id: str) -> str:
    return await gateway.place_order(
        "XBTUSD", "limit", "buy", base._AMOUNT, base._PRICE, cl_ord_id=cl_ord_id
    )


def _trade(core: Any, txid: str, tid: str, vol: str, price: str = "27000", seq: int = 1) -> None:
    detail = {
        "ordertxid": txid,
        "pair": "XBT/USD",
        "time": _TIME_WS,
        "type": "buy",
        "ordertype": "limit",
        "price": price,
        "cost": str(_D(vol) * _D(price)),
        "fee": "0.1",
        "vol": vol,
        "margin": "0",
    }
    core.on_own_trades(
        base.OwnTradesEvent.from_raw_frame([[{tid: detail}], "ownTrades", {"sequence": seq}])
    )


def _pending_for(core: Any, txid: str) -> list[Any]:
    return [key for key in core._pending_trades._items if key[0] == txid]


def _cum(sink: Sink) -> list[tuple[str, Decimal]]:
    return [(f.client_order_id, f.cumulative_filled) for f in sink.fills]


class TestEarlyGatewayFill:
    async def test_trade_before_placement_recorded_emits_once_with_tag(
        self, core: Any, gateway: KrakenGateway, rest: GatedRest
    ) -> None:
        sink = Sink()
        gateway.subscribe_leg_fills(sink.on_fill)
        rest.gates["mcl-1-0"] = asyncio.Event()
        task = asyncio.ensure_future(_place(gateway, "mcl-1-0"))
        await _settle()
        assert "mcl-1-0" in rest.in_flight  # AddOrder is pending: placement not yet recorded

        txid = "TX1-mcl-1-0"  # what the fake exchange will return
        _trade(core, txid, "T1", "0.2")
        await _settle()
        rest.gates["mcl-1-0"].set()
        assert await task == txid
        await _settle()

        assert _cum(sink) == [("mcl-1-0", _D("0.2"))]

        _trade(core, txid, "T1", "0.2", seq=2)
        await _settle()
        assert _cum(sink) == [("mcl-1-0", _D("0.2"))]


class TestFullyFilledLegReleasesState:
    async def test_full_leg_released_and_active_leg_survives_other_placements(
        self, core: Any, gateway: KrakenGateway
    ) -> None:
        sink = Sink()
        gateway.subscribe_leg_fills(sink.on_fill)
        early = await _place(gateway, "mcl-2-1")
        _trade(core, early, "E1", "0.1", seq=1)
        full = await _place(gateway, "mcl-2-0")
        _trade(core, full, "F1", "0.2", seq=2)
        _trade(core, full, "F2", "0.3", seq=3)
        await _settle()
        assert _cum(sink)[-2:] == [("mcl-2-0", _D("0.2")), ("mcl-2-0", _D("0.5"))]
        emitted = len(sink.fills)

        _trade(core, full, "F1", "0.2", seq=4)
        _trade(core, full, "F2", "0.3", seq=5)
        await _settle()
        assert len(sink.fills) == emitted
        assert _pending_for(core, full) == []

        for n in range(5000):
            await _place(gateway, f"p-{n}")

        _trade(core, early, "E2", "0.2", seq=6)
        await _settle()
        assert _cum(sink)[-1] == ("mcl-2-1", _D("0.3"))
        assert len(sink.fills) == emitted + 1


class TestReplayAfterHeavyTraffic:
    async def test_replayed_trade_does_not_inflate_cumulative(
        self, core: Any, gateway: KrakenGateway
    ) -> None:
        sink = Sink()
        gateway.subscribe_leg_fills(sink.on_fill)
        open_leg = await _place(gateway, "mcl-3-0")
        other = await _place(gateway, "mcl-3-1")
        _trade(core, open_leg, "T1", "0.2", seq=1)
        for n in range(5000):
            _trade(core, other, f"O{n}", "0.0001", seq=n + 2)
        await _settle()
        emitted = len(sink.fills)

        _trade(core, open_leg, "T1", "0.2", seq=6000)
        await _settle()
        assert len(sink.fills) == emitted

        _trade(core, open_leg, "T2", "0.3", seq=6001)
        await _settle()
        assert _cum(sink)[-1] == ("mcl-3-0", _D("0.5"))


class TestTagReuse:
    async def test_new_order_reusing_tag_emits_with_its_own_cumulative(
        self, core: Any, gateway: KrakenGateway
    ) -> None:
        sink = Sink()
        gateway.subscribe_leg_fills(sink.on_fill)
        a = await _place(gateway, "mcl-7-0")
        _trade(core, a, "A1", "0.8", seq=1)
        await _settle()
        b = await _place(gateway, "mcl-7-0")
        assert b != a
        _trade(core, b, "B1", "0.3", seq=2)
        await _settle()

        assert _cum(sink) == [("mcl-7-0", _D("0.8")), ("mcl-7-0", _D("0.3"))]


class TestAsyncInOrderDelivery:
    async def test_async_subscriber_sees_increasing_cumulative_never_concurrently(
        self, core: Any, gateway: KrakenGateway
    ) -> None:
        seen_order: list[Decimal] = []
        state = {"running": 0, "max": 0}

        async def callback(fill: Any) -> None:
            state["running"] += 1
            state["max"] = max(state["max"], state["running"])
            await asyncio.sleep(0.05 if not seen_order else 0)
            seen_order.append(fill.cumulative_filled)
            state["running"] -= 1

        gateway.subscribe_leg_fills(callback)
        txid = await _place(gateway, "mcl-5-0")
        _trade(core, txid, "T1", "0.2", seq=1)
        _trade(core, txid, "T2", "0.3", seq=2)
        for _ in range(100):
            if len(seen_order) >= 2:
                break
            await asyncio.sleep(0.01)

        assert seen_order == [_D("0.2"), _D("0.5")]
        assert state["max"] == 1


def _hub_tasks(gateway: Any) -> set[Any]:
    hub = getattr(gateway, "_leg_fill_hub", None)
    return {t for t in getattr(hub, "tasks", set()) if not t.done()}


class TestCoalescing:
    async def test_blocked_subscriber_gets_in_flight_then_newest_per_leg(
        self, core: Any, gateway: KrakenGateway
    ) -> None:
        release = asyncio.Event()
        observed: list[tuple[str, Decimal]] = []
        first = True

        async def callback(fill: Any) -> None:
            nonlocal first
            if first:
                first = False
                await release.wait()
            observed.append((fill.client_order_id, fill.cumulative_filled))

        gateway.subscribe_leg_fills(callback)
        one = await _place(gateway, "mcl-8-0")
        two = await _place(gateway, "mcl-8-1")
        for n, vol in enumerate(["0.1", "0.1", "0.1", "0.1"]):
            _trade(core, one, f"A{n}", vol, seq=n + 1)
        await _settle()
        _trade(core, two, "B0", "0.1", seq=10)
        _trade(core, two, "B1", "0.1", seq=11)
        await _settle()
        release.set()
        for _ in range(20):
            await _settle()

        leg_one = [c for cid, c in observed if cid == "mcl-8-0"]
        leg_two = [c for cid, c in observed if cid == "mcl-8-1"]
        assert leg_one == [_D("0.1"), _D("0.4")]
        assert leg_two == [_D("0.2")]
        assert _hub_tasks(gateway) == set()

    async def test_unsubscribe_discards_pending_and_leaves_no_drain_task(
        self, core: Any, gateway: KrakenGateway
    ) -> None:
        release = asyncio.Event()
        observed: list[Decimal] = []
        first = True

        async def callback(fill: Any) -> None:
            nonlocal first
            if first:
                first = False
                await release.wait()
            observed.append(fill.cumulative_filled)

        unsubscribe = gateway.subscribe_leg_fills(callback)
        txid = await _place(gateway, "mcl-9-0")
        for n in range(3):
            _trade(core, txid, f"T{n}", "0.1", seq=n + 1)
        await _settle()
        unsubscribe()
        release.set()
        for _ in range(20):
            await _settle()

        assert observed == [_D("0.1")]
        assert _hub_tasks(gateway) == set()


class TestLogSafety:
    async def test_cid_with_newline_is_escaped_in_callback_error_log(
        self, core: Any, gateway: KrakenGateway, caplog: pytest.LogCaptureFixture
    ) -> None:
        def boom(fill: Any) -> None:
            raise RuntimeError("boom")

        gateway.subscribe_leg_fills(boom)
        txid = await _place(gateway, "mcl-1-0\nFORGED")
        with caplog.at_level(logging.DEBUG):
            _trade(core, txid, "T1", "0.2")
            await _settle()

        messages = [r.getMessage() for r in caplog.records if "FORGED" in r.getMessage()]
        assert messages, "callback failure was not logged with the cid"
        assert all("\n" not in m for m in messages)
