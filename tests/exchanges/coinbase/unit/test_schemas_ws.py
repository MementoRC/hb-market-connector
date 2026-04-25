"""Tests for Coinbase WS message schema models."""

import json
from pathlib import Path

import pytest

from market_connector.exchanges.coinbase.schemas.ws import (
    Level2Event,
    Level2Update,
    MarketTradesEvent,
    UserEvent,
    WsMessage,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "ws"


class TestWsMessage:
    def test_level2_snapshot_parses(self):
        data = json.loads((FIXTURES / "level2_snapshot.json").read_text())
        msg = WsMessage.model_validate(data)
        assert msg.channel == "l2_data"
        assert len(msg.events) == 1

    def test_market_trades_parses(self):
        data = json.loads((FIXTURES / "market_trades.json").read_text())
        msg = WsMessage.model_validate(data)
        assert msg.channel == "market_trades"
        assert msg.sequence_num == 1

    def test_user_order_parses(self):
        data = json.loads((FIXTURES / "user_order.json").read_text())
        msg = WsMessage.model_validate(data)
        assert msg.channel == "user"
        assert msg.sequence_num == 5

    def test_ws_message_defaults(self):
        msg = WsMessage.model_validate(
            {
                "channel": "ticker",
                "timestamp": "2026-04-24T12:00:00Z",
                "events": [],
            }
        )
        assert msg.client_id == ""
        assert msg.sequence_num == 0

    def test_ws_message_is_frozen(self):
        from pydantic import ValidationError  # noqa: PLC0415

        data = json.loads((FIXTURES / "level2_snapshot.json").read_text())
        msg = WsMessage.model_validate(data)
        with pytest.raises(ValidationError):
            msg.channel = "changed"  # type: ignore[misc]

    def test_ws_message_ignores_extra_fields(self):
        data = {
            "channel": "heartbeats",
            "timestamp": "2026-04-24T12:00:00Z",
            "events": [],
            "unknown_future_field": "ignored",
        }
        msg = WsMessage.model_validate(data)
        assert msg.channel == "heartbeats"


class TestLevel2Update:
    def test_level2_update_fields(self):
        data = {
            "side": "bid",
            "event_time": "2026-04-24T12:00:00Z",
            "price_level": "50000.00",
            "new_quantity": "0.5",
        }
        update = Level2Update.model_validate(data)
        assert update.side == "bid"
        assert update.price_level == "50000.00"
        assert update.new_quantity == "0.5"


class TestLevel2Event:
    def test_level2_event_from_fixture(self):
        raw = json.loads((FIXTURES / "level2_snapshot.json").read_text())
        event_data = raw["events"][0]
        event = Level2Event.model_validate(event_data)
        assert event.type == "snapshot"
        assert event.product_id == "BTC-USD"
        assert len(event.updates) == 2
        assert event.updates[0].side == "bid"
        assert event.updates[1].side == "offer"


class TestMarketTradesEvent:
    def test_market_trades_event_from_fixture(self):
        raw = json.loads((FIXTURES / "market_trades.json").read_text())
        event_data = raw["events"][0]
        event = MarketTradesEvent.model_validate(event_data)
        assert event.type == "update"
        assert len(event.trades) == 1
        trade = event.trades[0]
        assert trade.trade_id == "trade-001"
        assert trade.product_id == "BTC-USD"
        assert trade.price == "50000.00"
        assert trade.side == "BUY"


class TestUserEvent:
    def test_user_event_from_fixture(self):
        raw = json.loads((FIXTURES / "user_order.json").read_text())
        event_data = raw["events"][0]
        event = UserEvent.model_validate(event_data)
        assert event.type == "snapshot"
        assert len(event.orders) == 1
        order = event.orders[0]
        assert order.order_id == "order-001"
        assert order.product_id == "BTC-USD"
        assert order.status == "OPEN"
        assert order.order_side == "BUY"
        assert order.order_type == "LIMIT"
