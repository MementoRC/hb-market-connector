"""Private Kraken WS v1 frames through the STOCK decoder and the gateway (issue #274), RED.

Kraken private frames are ``[[{id: detail}], "ownTrades" | "openOrders", {"sequence": N}]``.
Today the stock decoder reads the trailing ``{"sequence": N}`` dict as the *pair* (unhashable in
the routing table) and the channel name as the payload, so no private frame reaches a handler.

Pinned: the stock decoder yields a DATA message on channel ``ownTrades`` / ``openOrders`` with
``pair is None`` (hashable routing key); a frame pushed as JSON text on the private socket reaches
the handler registered by ``subscribe_own_trades`` / ``subscribe_open_orders`` as a valid
``OwnTradesEvent`` / ``OpenOrdersEvent`` with its data and sequence; public frames decode as
before (regression).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from market_connector.exchanges.kraken.schemas.ws import OpenOrdersEvent, OwnTradesEvent
from market_connector.exchanges.kraken.specs import KRAKEN_WS_DECODER
from market_connector.hb_compat.event_bus import WsRoutingTable
from market_connector.ws_models.decoder import WsMessageKind
from tests.exchanges.kraken.unit.test_private_ws_274 import open_rig, until

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "ws"


def _load(name: str) -> Any:
    with (_FIXTURES / name).open() as fh:
        return json.load(fh)


class TestStockDecoderPrivateFrames:
    @pytest.mark.parametrize(
        ("fixture", "channel"),
        [("own_trades.json", "ownTrades"), ("open_orders.json", "openOrders")],
    )
    def test_private_frame_is_data_on_its_channel_with_no_pair(
        self, fixture: str, channel: str
    ) -> None:
        msg = KRAKEN_WS_DECODER.decode(_load(fixture))
        assert msg.kind is WsMessageKind.DATA
        assert msg.channel == channel
        assert msg.pair is None

    @pytest.mark.parametrize(
        ("fixture", "channel"),
        [("own_trades.json", "ownTrades"), ("open_orders.json", "openOrders")],
    )
    def test_decoded_private_frame_routes_to_a_channel_wide_handler(
        self, fixture: str, channel: str
    ) -> None:
        msg = KRAKEN_WS_DECODER.decode(_load(fixture))
        table = WsRoutingTable(name="decode-274")
        seen: list[Any] = []
        table.subscribe(channel, None, seen.append)
        assert table.route(msg.channel or "", msg.pair, msg.payload) is True
        assert len(seen) == 1


class TestPublicFramesUnchanged:
    def test_book_snapshot_still_routes_by_channel_and_pair(self) -> None:
        raw = _load("book_snapshot.json")
        msg = KRAKEN_WS_DECODER.decode(raw)
        assert msg.kind is WsMessageKind.DATA
        assert msg.channel == "book-25"
        assert msg.pair == "XBT/USD"
        assert msg.payload == raw[1]

    def test_trade_still_routes_by_channel_and_pair(self) -> None:
        raw = _load("trade_event.json")
        msg = KRAKEN_WS_DECODER.decode(raw)
        assert msg.kind is WsMessageKind.DATA
        assert msg.channel == "trade"
        assert msg.pair == "XBT/USD"
        assert msg.payload == raw[1]

    def test_ack_and_control_frames_still_classified(self) -> None:
        ack = KRAKEN_WS_DECODER.decode(_load("subscribe_ack.json"))
        assert ack.kind is WsMessageKind.SUBSCRIBE_ACK
        assert KRAKEN_WS_DECODER.decode(_load("heartbeat.json")).kind is WsMessageKind.HEARTBEAT
        assert KRAKEN_WS_DECODER.decode(_load("system_status.json")).kind is WsMessageKind.UNKNOWN


class TestPrivateFramesReachHandlers:
    async def test_own_trades_frame_reaches_the_handler_as_an_event(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        received: list[OwnTradesEvent] = []
        async with (
            open_rig(monkeypatch) as rig,
            await rig.gw.subscribe_own_trades(received.append),
        ):
            rig.priv.push(_load("own_trades.json"))
            await until(lambda: len(received) == 1)
        event = received[0]
        assert isinstance(event, OwnTradesEvent)
        assert event.sequence == 2948
        assert set(event.trades) == {"TDLH43-DVQXD-2KHVYY", "TDLH43-DVQXD-2KHVZZ"}
        assert event.trades["TDLH43-DVQXD-2KHVYY"].type == "buy"
        assert event.trades["TDLH43-DVQXD-2KHVZZ"].price == "100000.00000"

    async def test_open_orders_frame_reaches_the_handler_as_an_event(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        received: list[OpenOrdersEvent] = []
        async with (
            open_rig(monkeypatch) as rig,
            await rig.gw.subscribe_open_orders(received.append),
        ):
            rig.priv.push(_load("open_orders.json"))
            await until(lambda: len(received) == 1)
        event = received[0]
        assert isinstance(event, OpenOrdersEvent)
        assert event.sequence == 234
        order = event.orders["OGTT3Y-C6I3P-XRI6HX"]
        assert order.status == "open"
        assert order.vol == "10.00345345"

    async def test_frames_for_one_private_channel_do_not_reach_the_other(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        own: list[OwnTradesEvent] = []
        opened: list[OpenOrdersEvent] = []
        async with (
            open_rig(monkeypatch) as rig,
            await rig.gw.subscribe_own_trades(own.append),
            await rig.gw.subscribe_open_orders(opened.append),
        ):
            rig.priv.push(_load("open_orders.json"))
            rig.priv.push(_load("own_trades.json"))
            await until(lambda: len(own) == 1 and len(opened) == 1)
        assert own[0].sequence == 2948
        assert opened[0].sequence == 234
