"""RED acceptance tests for #323 (Kraken): late-subscriber ``LegFill`` replay.

Contract: a subscriber that calls ``subscribe_leg_fills`` AFTER own-trades were booked receives
the latest known ``LegFill`` of every gateway-placed leg the gateway knows, active (partially
filled) or terminal (fully filled).

* Replay is scheduled, not inline: the callback is not invoked before ``subscribe_leg_fills``
  returns; it runs after the loop is yielded to (sync and async callbacks alike).
* Each new subscriber gets the replay once; subscribers already attached get no duplicate.
* A live fill after the replay is delivered normally and stays strictly increasing; a live
  fill whose cumulative equals the replayed value (here a zero-volume trade) is not
  re-emitted to that subscriber.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest

from market_connector.exchanges.kraken.schemas.ws import OwnTradesEvent
from tests.exchanges.kraken.unit import test_kraken_connector_core_211 as base

if TYPE_CHECKING:
    from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway
    from market_connector.hb_compat.event_bus import EventBus
    from tests.exchanges.kraken.unit.test_kraken_connector_core_211 import Recorder

gateway = base.gateway
bus = base.bus
seen = base.seen
rest = base.rest

_D = Decimal
_TIME_WS = "1700000000.5"
TAG_A = "mcl-1-0"
TAG_B = "mcl-1-1"


@pytest.fixture
async def core(gateway: KrakenGateway, bus: EventBus, seen: Recorder) -> Any:  # noqa: F811
    cls = __import__(base._CORE_MOD, fromlist=["KrakenConnectorCore"]).KrakenConnectorCore
    c = cls(gateway, event_bus=bus)
    await c.update_trading_rules()
    return c


class Sink:
    def __init__(self) -> None:
        self.fills: list[Any] = []

    def on_fill(self, fill: Any) -> None:
        self.fills.append(fill)

    async def on_fill_async(self, fill: Any) -> None:
        self.fills.append(fill)

    @property
    def cumulative(self) -> list[Decimal]:
        return [f.cumulative_filled for f in self.fills]


def _callback(sink: Sink, kind: str) -> Any:
    return sink.on_fill if kind == "sync" else sink.on_fill_async


async def _settle() -> None:
    for _ in range(4):
        await asyncio.sleep(0)


async def _place(gateway: KrakenGateway, tag: str) -> str:  # noqa: F811
    """Place a 0.5 BTC gateway leg; returns its txid (OTXID-1, OTXID-2, ... in order)."""
    return await gateway.place_order(
        "XBTUSD", "limit", "buy", base._AMOUNT, base._PRICE, cl_ord_id=tag
    )


def _trade(core: Any, txid: str, trade_id: str, vol: str, price: str, seq: int) -> None:
    """Book one WS own-trade of the order ``txid`` through the core."""
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
        OwnTradesEvent.from_raw_frame([[{trade_id: detail}], "ownTrades", {"sequence": seq}])
    )


async def _partial_leg(gateway: KrakenGateway, core: Any) -> str:  # noqa: F811
    """A leg with 0.2 of 0.5 filled and no subscriber attached; returns its txid."""
    txid = await _place(gateway, TAG_A)
    _trade(core, txid, "T1", "0.2", "27000", seq=1)
    await _settle()
    return txid


class TestReplayToLateSubscriber:
    @pytest.mark.parametrize("kind", ["sync", "async"])
    async def test_active_leg_latest_cumulative_is_replayed(
        self,
        gateway: KrakenGateway,  # noqa: F811
        core: Any,
        kind: str,
    ) -> None:
        await _partial_leg(gateway, core)
        sink = Sink()

        gateway.subscribe_leg_fills(_callback(sink, kind))
        await _settle()

        assert [(f.client_order_id, f.cumulative_filled, f.fill_id) for f in sink.fills] == [
            (TAG_A, _D("0.2"), "T1")
        ]

    async def test_terminal_leg_final_total_is_replayed(
        self,
        gateway: KrakenGateway,  # noqa: F811
        core: Any,
    ) -> None:
        txid = await _place(gateway, TAG_A)
        _trade(core, txid, "T1", "0.2", "27000", seq=1)
        _trade(core, txid, "T2", "0.3", "27010", seq=2)
        await _settle()
        sink = Sink()

        gateway.subscribe_leg_fills(sink.on_fill)
        await _settle()

        assert [(f.client_order_id, f.cumulative_filled, f.fill_id) for f in sink.fills] == [
            (TAG_A, _D("0.5"), "T2")
        ]

    async def test_every_known_leg_active_and_terminal_is_replayed(
        self,
        gateway: KrakenGateway,  # noqa: F811
        core: Any,
    ) -> None:
        txid_a = await _place(gateway, TAG_A)
        txid_b = await _place(gateway, TAG_B)
        _trade(core, txid_a, "A1", "0.2", "27000", seq=1)
        _trade(core, txid_b, "B1", "0.5", "27005", seq=2)
        await _settle()
        sink = Sink()

        gateway.subscribe_leg_fills(sink.on_fill)
        await _settle()

        assert sorted((f.client_order_id, f.cumulative_filled) for f in sink.fills) == [
            (TAG_A, _D("0.2")),
            (TAG_B, _D("0.5")),
        ]

    @pytest.mark.parametrize("kind", ["sync", "async"])
    async def test_replay_is_scheduled_not_inline(
        self,
        gateway: KrakenGateway,  # noqa: F811
        core: Any,
        kind: str,
    ) -> None:
        await _partial_leg(gateway, core)
        sink = Sink()

        gateway.subscribe_leg_fills(_callback(sink, kind))

        assert sink.fills == []
        await _settle()
        assert sink.cumulative == [_D("0.2")]


class TestIdempotenceAndIsolation:
    async def test_attached_subscriber_gets_no_duplicate_when_another_joins(
        self,
        gateway: KrakenGateway,  # noqa: F811
        core: Any,
    ) -> None:
        await _partial_leg(gateway, core)
        first, second = Sink(), Sink()
        gateway.subscribe_leg_fills(first.on_fill)
        await _settle()

        gateway.subscribe_leg_fills(second.on_fill)
        await _settle()

        assert first.cumulative == [_D("0.2")]
        assert second.cumulative == [_D("0.2")]

    async def test_live_fill_after_replay_is_delivered_and_strictly_increasing(
        self,
        gateway: KrakenGateway,  # noqa: F811
        core: Any,
    ) -> None:
        txid = await _partial_leg(gateway, core)
        sink = Sink()
        gateway.subscribe_leg_fills(sink.on_fill)
        await _settle()

        _trade(core, txid, "T2", "0.3", "27010", seq=2)
        await _settle()

        assert sink.cumulative == [_D("0.2"), _D("0.5")]

    async def test_live_fill_equal_to_replayed_value_is_not_re_emitted(
        self,
        gateway: KrakenGateway,  # noqa: F811
        core: Any,
    ) -> None:
        txid = await _partial_leg(gateway, core)
        sink = Sink()
        gateway.subscribe_leg_fills(sink.on_fill)
        await _settle()

        _trade(core, txid, "T2", "0", "27010", seq=2)
        await _settle()

        assert [f.fill_id for f in sink.fills] == ["T1"]
