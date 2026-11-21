"""RED acceptance tests for #249 (Kraken part): per-leg cumulative ``LegFill`` push stream.

Contract (agreed with strategy-framework; this package must not import it, ADR 0001):

* ``market_connector.protocols.LegFill``: frozen dataclass ``client_order_id: str``,
  ``cumulative_filled: Decimal``, ``last_price: Decimal``, ``timestamp: float`` (unix
  seconds), ``fill_id: str | None = None``.
* ``market_connector.protocols.SupportsLegFills``: ``@runtime_checkable`` Protocol with::

      def subscribe_leg_fills(
          self, callback: Callable[[LegFill], Awaitable[None] | None]
      ) -> Callable[[], None]

  Chosen signature: a plain (non-async) call, because the existing ``subscribe_*`` helpers in
  ``mixins/subscriptions.py`` are async context managers bound to a WS channel, which does not
  fit a connector-level stream fed by both WS and REST. The callback may be sync or async
  (an awaitable result is awaited). The return value is the unsubscribe callable (idempotent).
* Both ``LegFill`` and ``SupportsLegFills`` are exported from the package root, and
  ``KrakenGateway`` satisfies ``isinstance(gw, SupportsLegFills)``.

Kraken semantics pinned here:

* The leg's ``client_order_id`` is the EXACT string the placing call returned, never
  ``str(userref)``: for core-placed orders the ``hbot-<side>-<uuid>`` id ``buy``/``sell``
  returned; for ``KrakenGateway.place_order`` orders the value ``place_order`` returned
  (today the first txid, NOT the ``cl_ord_id``; the tests assert against the return value).
* Each trade booked for such an order (WS ``ownTrades`` via ``core.on_own_trades`` or REST
  QueryOrders+QueryTrades via ``core._poll_once``) emits exactly one LegFill:
  ``cumulative_filled`` is the running total base volume (strictly increasing),
  ``last_price`` the trade price, ``timestamp`` the trade time, ``fill_id`` the trade txid.
* A trade is emitted once: the core already dedups by trade id (#286), so a WS duplicate or
  a WS+REST replay emits nothing more.
* A trade whose order is unknown to the connector has no cid and is NOT emitted.

Trades are driven through ``KrakenConnectorCore`` (the only trade-ingestion path), and the
subscription is taken on the ``KrakenGateway`` it wraps. The core's orders are 0.5 BTC, so
the two partial fills are 0.2 then 0.3 (the issue's 0.4/0.6 on a 1.0 order, scaled).
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest

from tests.exchanges.kraken.unit import test_kraken_connector_core_211 as base

if TYPE_CHECKING:
    from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway
    from market_connector.hb_compat.event_bus import EventBus
    from tests.exchanges.kraken.unit.test_kraken_connector_core_211 import (
        FakeKrakenRest,
        Recorder,
    )

gateway = base.gateway
bus = base.bus
seen = base.seen
rest = base.rest

_D = Decimal
_TXID = base._TXID
_TIME = 1700000000.5
_TIME_WS = "1700000000.5"


def _proto(name: str) -> Any:
    return getattr(importlib.import_module("market_connector.protocols"), name)


@pytest.fixture
async def core(gateway: KrakenGateway, bus: EventBus, seen: Recorder) -> Any:  # noqa: F811
    cls = importlib.import_module(base._CORE_MOD).KrakenConnectorCore
    c = cls(gateway, event_bus=bus)
    await c.update_trading_rules()
    return c


class Sink:
    """Collects LegFills; ``on_fill`` is the sync callback, ``on_fill_async`` the async one."""

    def __init__(self) -> None:
        self.fills: list[Any] = []

    def on_fill(self, fill: Any) -> None:
        self.fills.append(fill)

    async def on_fill_async(self, fill: Any) -> None:
        self.fills.append(fill)


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def _placed(core: Any, seen: Recorder, rest: FakeKrakenRest) -> str:  # noqa: F811
    """Place a 0.5 order via core.buy; return its leg cid (the id ``buy`` returned)."""
    return await base._place(core, seen)


async def _gateway_placed(gateway: KrakenGateway, **kwargs: Any) -> str:  # noqa: F811
    """Place a 0.5 order straight on the gateway (the core never tracks it)."""
    return await gateway.place_order("XBTUSD", "limit", "buy", base._AMOUNT, base._PRICE, **kwargs)


def _ws(core: Any, tid: str, vol: str, price: str, seq: int = 1) -> None:
    cost = str(_D(vol) * _D(price))
    core.on_own_trades(
        base._ws_trades(tid, vol=vol, price=price, cost=cost, fee="0.1", time=_TIME_WS, seq=seq)
    )


async def _rest(core: Any, rest: FakeKrakenRest, *fills: tuple[str, str, str]) -> None:  # noqa: F811
    """REST poll that sees every ``(txid, vol, price)`` in ``fills`` on the order."""
    total = sum((_D(v) for _, v, _ in fills), _D(0))
    for tid, vol, price in fills:
        rest.trades[tid] = base._trade_info(
            vol=vol, price=price, cost=str(_D(vol) * _D(price)), fee="0.1", time=_TIME
        )
    rest.orders[_TXID] = base._order_info(
        status="open",
        vol_exec=str(total),
        cost=str(total * _D(fills[-1][2])),
        price=fills[-1][2],
        trades=tuple(t for t, _, _ in fills),
    )
    await core._poll_once()
    await _settle()


def _shape(sink: Sink) -> list[tuple[str, Decimal, Decimal, str | None]]:
    return [(f.client_order_id, f.cumulative_filled, f.last_price, f.fill_id) for f in sink.fills]


class TestLegFillType:
    def test_is_frozen_dataclass_with_agreed_fields(self) -> None:
        leg_fill = _proto("LegFill")
        assert dataclasses.is_dataclass(leg_fill)
        fields = {f.name: f for f in dataclasses.fields(leg_fill)}
        assert list(fields) == [
            "client_order_id",
            "cumulative_filled",
            "last_price",
            "timestamp",
            "fill_id",
        ]
        assert fields["fill_id"].default is None

        instance = leg_fill("c1", _D("0.4"), _D("100"), 1.5)
        assert instance.fill_id is None
        with pytest.raises(dataclasses.FrozenInstanceError):
            instance.cumulative_filled = _D(1)


class TestEmission:
    @pytest.mark.parametrize("via", ["ws", "rest"])
    async def test_two_partial_fills_emit_cumulative_legfills(
        self,
        core: Any,
        gateway: KrakenGateway,  # noqa: F811
        rest: FakeKrakenRest,  # noqa: F811
        seen: Recorder,
        via: str,
    ) -> None:
        sink = Sink()
        gateway.subscribe_leg_fills(sink.on_fill)
        cid = await _placed(core, seen, rest)

        if via == "ws":
            _ws(core, "T1", "0.2", "27000", seq=1)
            await _settle()
            _ws(core, "T2", "0.3", "27010", seq=2)
            await _settle()
        else:
            await _rest(core, rest, ("T1", "0.2", "27000"))
            await _rest(core, rest, ("T1", "0.2", "27000"), ("T2", "0.3", "27010"))

        assert _shape(sink) == [
            (cid, _D("0.2"), _D("27000"), "T1"),
            (cid, _D("0.5"), _D("27010"), "T2"),
        ]
        assert all(isinstance(f, _proto("LegFill")) for f in sink.fills)
        assert [f.timestamp for f in sink.fills] == [_TIME, _TIME]

    async def test_async_callback_is_awaited(
        self,
        core: Any,
        gateway: KrakenGateway,  # noqa: F811
        rest: FakeKrakenRest,  # noqa: F811
        seen: Recorder,
    ) -> None:
        sink = Sink()
        gateway.subscribe_leg_fills(sink.on_fill_async)
        cid = await _placed(core, seen, rest)

        _ws(core, "T1", "0.2", "27000")
        await _settle()

        assert _shape(sink) == [(cid, _D("0.2"), _D("27000"), "T1")]

    async def test_amounts_are_decimal_never_float(
        self,
        core: Any,
        gateway: KrakenGateway,  # noqa: F811
        rest: FakeKrakenRest,  # noqa: F811
        seen: Recorder,
    ) -> None:
        sink = Sink()
        gateway.subscribe_leg_fills(sink.on_fill)
        await _placed(core, seen, rest)

        _ws(core, "T1", "0.2", "27000.1")
        await _settle()
        await _rest(core, rest, ("T1", "0.2", "27000.1"), ("T2", "0.3", "27010.7"))

        assert len(sink.fills) == 2
        for fill in sink.fills:
            assert type(fill.cumulative_filled) is Decimal
            assert type(fill.last_price) is Decimal
            assert isinstance(fill.timestamp, float)
        assert sink.fills[0].last_price == _D("27000.1")
        assert sink.fills[1].cumulative_filled == _D("0.5")


class TestNoDuplicateEmission:
    @pytest.mark.parametrize("replay", ["ws", "rest"])
    async def test_same_trade_delivered_twice_emits_once(
        self,
        core: Any,
        gateway: KrakenGateway,  # noqa: F811
        rest: FakeKrakenRest,  # noqa: F811
        seen: Recorder,
        replay: str,
    ) -> None:
        sink = Sink()
        gateway.subscribe_leg_fills(sink.on_fill)
        cid = await _placed(core, seen, rest)

        _ws(core, "T1", "0.2", "27000")
        await _settle()
        if replay == "ws":
            _ws(core, "T1", "0.2", "27000", seq=2)
            await _settle()
        else:
            await _rest(core, rest, ("T1", "0.2", "27000"))

        assert _shape(sink) == [(cid, _D("0.2"), _D("27000"), "T1")]

    async def test_rest_replay_then_new_ws_trade_keeps_cumulative_strictly_increasing(
        self,
        core: Any,
        gateway: KrakenGateway,  # noqa: F811
        rest: FakeKrakenRest,  # noqa: F811
        seen: Recorder,
    ) -> None:
        sink = Sink()
        gateway.subscribe_leg_fills(sink.on_fill)
        cid = await _placed(core, seen, rest)

        await _rest(core, rest, ("T1", "0.2", "27000"))
        await _rest(core, rest, ("T1", "0.2", "27000"))
        _ws(core, "T2", "0.3", "27010")
        await _settle()

        assert _shape(sink) == [
            (cid, _D("0.2"), _D("27000"), "T1"),
            (cid, _D("0.5"), _D("27010"), "T2"),
        ]


class TestGatewayPlaced:
    async def test_two_partial_fills_use_exact_place_order_return_as_cid(
        self,
        core: Any,
        gateway: KrakenGateway,  # noqa: F811
    ) -> None:
        sink = Sink()
        gateway.subscribe_leg_fills(sink.on_fill)
        cid = await _gateway_placed(gateway, cl_ord_id="mcl-1-0")

        _ws(core, "T1", "0.2", "27000", seq=1)
        await _settle()
        _ws(core, "T2", "0.3", "27010", seq=2)
        await _settle()

        assert _shape(sink) == [
            (cid, _D("0.2"), _D("27000"), "T1"),
            (cid, _D("0.5"), _D("27010"), "T2"),
        ]
        assert not cid.startswith("hbot-")

    async def test_same_trade_delivered_twice_emits_once(
        self,
        core: Any,
        gateway: KrakenGateway,  # noqa: F811
    ) -> None:
        sink = Sink()
        gateway.subscribe_leg_fills(sink.on_fill)
        cid = await _gateway_placed(gateway, cl_ord_id="mcl-1-0")

        _ws(core, "T1", "0.2", "27000", seq=1)
        await _settle()
        _ws(core, "T1", "0.2", "27000", seq=2)
        await _settle()

        assert _shape(sink) == [(cid, _D("0.2"), _D("27000"), "T1")]

    async def test_negative_link_userref_without_cl_ord_id_uses_exact_return(
        self,
        core: Any,
        gateway: KrakenGateway,  # noqa: F811
        rest: FakeKrakenRest,  # noqa: F811
    ) -> None:
        sink = Sink()
        gateway.subscribe_leg_fills(sink.on_fill)
        cid = await _gateway_placed(gateway, userref=-5)

        assert rest.bodies("add_order")[0]["userref"] == -5
        _ws(core, "T1", "0.2", "27000")
        await _settle()

        assert _shape(sink) == [(cid, _D("0.2"), _D("27000"), "T1")]
        assert cid != "-5"


class TestGatewayLegIsNotBuffered:
    async def test_claimed_trades_never_enter_core_pending_buffer(
        self,
        core: Any,
        gateway: KrakenGateway,  # noqa: F811
    ) -> None:
        """A gateway-placed txid is claimed by the leg-fill hub: the core must not buffer it."""
        sink = Sink()
        gateway.subscribe_leg_fills(sink.on_fill)
        cid = await _gateway_placed(gateway, cl_ord_id="mcl-1-0")

        for n in range(1, 201):
            _ws(core, f"T{n}", "0.001", "27000", seq=n)
        await _settle()

        buffered = [key for key in core._pending_trades._items if key[0] == _TXID]
        assert buffered == []
        assert len(sink.fills) == 200
        assert {f.client_order_id for f in sink.fills} == {cid}
        cumulative = [f.cumulative_filled for f in sink.fills]
        assert all(a < b for a, b in zip(cumulative, cumulative[1:], strict=False))
        assert cumulative[-1] == _D("0.2")


class TestPreAckRace:
    async def test_ws_trade_before_placement_ack_emits_once_with_hbot_cid(
        self,
        core: Any,
        gateway: KrakenGateway,  # noqa: F811
        seen: Recorder,
    ) -> None:
        sink = Sink()
        gateway.subscribe_leg_fills(sink.on_fill)

        cid = core.buy(
            trading_pair=base._PAIR,
            amount=base._AMOUNT,
            order_type=base.OrderType.LIMIT,
            price=base._PRICE,
            position_action=base.PositionAction.NIL,
        )
        # The AddOrder task has not run yet: the trade's txid is still unknown to the core.
        _ws(core, "T1", "0.2", "27000")
        await _settle()
        await seen.wait(base.BUY_CREATED)
        await _settle()

        assert cid.startswith("hbot-buy-")
        assert _shape(sink) == [(cid, _D("0.2"), _D("27000"), "T1")]


class TestUntaggedOrders:
    async def test_trade_for_order_without_a_cid_is_not_emitted(
        self,
        core: Any,
        gateway: KrakenGateway,  # noqa: F811
        rest: FakeKrakenRest,  # noqa: F811
        seen: Recorder,
    ) -> None:
        """No cid exists for a txid the connector never tagged or tracked: nothing to key on."""
        sink = Sink()
        gateway.subscribe_leg_fills(sink.on_fill)
        await _placed(core, seen, rest)

        detail = {
            "ordertxid": "OTHER-TXID",
            "pair": "XBT/USD",
            "time": _TIME_WS,
            "type": "buy",
            "ordertype": "limit",
            "price": "27000",
            "cost": "5400",
            "fee": "0.1",
            "vol": "0.2",
            "margin": "0",
        }
        core.on_own_trades(
            base.OwnTradesEvent.from_raw_frame([[{"TX": detail}], "ownTrades", {"sequence": 1}])
        )
        await _settle()

        assert sink.fills == []


class TestUnsubscribe:
    async def test_unsubscribe_stops_delivery_and_is_idempotent(
        self,
        core: Any,
        gateway: KrakenGateway,  # noqa: F811
        rest: FakeKrakenRest,  # noqa: F811
        seen: Recorder,
    ) -> None:
        kept, dropped = Sink(), Sink()
        gateway.subscribe_leg_fills(kept.on_fill)
        unsubscribe = gateway.subscribe_leg_fills(dropped.on_fill)
        cid = await _placed(core, seen, rest)

        _ws(core, "T1", "0.2", "27000", seq=1)
        await _settle()
        unsubscribe()
        unsubscribe()
        _ws(core, "T2", "0.3", "27010", seq=2)
        await _settle()

        assert _shape(dropped) == [(cid, _D("0.2"), _D("27000"), "T1")]
        assert [f.fill_id for f in kept.fills] == ["T1", "T2"]


class TestExportsAndProtocol:
    def test_exported_from_package_root(self) -> None:
        root = importlib.import_module("market_connector")
        protocols = importlib.import_module("market_connector.protocols")
        assert root.LegFill is protocols.LegFill
        assert root.SupportsLegFills is protocols.SupportsLegFills

    def test_gateway_satisfies_supports_leg_fills(self, gateway: KrakenGateway) -> None:  # noqa: F811
        assert isinstance(gateway, _proto("SupportsLegFills"))
