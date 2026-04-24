from decimal import Decimal

import pytest
from pydantic import ValidationError

from market_connector.primitives import (
    OpenOrder,
    OrderBookSnapshot,
    OrderBookUpdate,
    OrderType,
    TradeEvent,
    TradeType,
)


class TestEnums:
    def test_order_types(self) -> None:
        assert OrderType.LIMIT == "LIMIT"
        assert OrderType.MARKET == "MARKET"
        assert OrderType.LIMIT_MAKER == "LIMIT_MAKER"

    def test_trade_types(self) -> None:
        assert TradeType.BUY == "BUY"
        assert TradeType.SELL == "SELL"


class TestOpenOrder:
    def test_create(self) -> None:
        order = OpenOrder(
            client_order_id="c1",
            exchange_order_id="e1",
            trading_pair="BTC-USDT",
            order_type=OrderType.LIMIT,
            side=TradeType.BUY,
            amount=Decimal("1.5"),
            price=Decimal("50000"),
            filled_amount=Decimal("0"),
            status="OPEN",
        )
        assert order.trading_pair == "BTC-USDT"
        assert order.amount == Decimal("1.5")

    def test_frozen(self) -> None:
        order = OpenOrder(
            client_order_id="c1",
            exchange_order_id="e1",
            trading_pair="BTC-USDT",
            order_type=OrderType.LIMIT,
            side=TradeType.BUY,
            amount=Decimal("1.5"),
            price=Decimal("50000"),
            filled_amount=Decimal("0"),
            status="OPEN",
        )
        with pytest.raises(ValidationError):
            order.status = "FILLED"


class TestTradeEvent:
    def test_create(self) -> None:
        event = TradeEvent(
            exchange_trade_id="t1",
            trading_pair="ETH-USD",
            price=Decimal("3000.50"),
            amount=Decimal("2.0"),
            side=TradeType.SELL,
            timestamp=1700000000.0,
        )
        assert event.exchange_trade_id == "t1"
        assert event.side == TradeType.SELL

    def test_frozen(self) -> None:
        event = TradeEvent(
            exchange_trade_id="t1",
            trading_pair="ETH-USD",
            price=Decimal("3000"),
            amount=Decimal("1"),
            side=TradeType.BUY,
            timestamp=1700000000.0,
        )
        with pytest.raises(ValidationError):
            event.price = Decimal("9999")


class TestOrderBookSnapshot:
    def test_create(self) -> None:
        snap = OrderBookSnapshot(
            trading_pair="BTC-USDT",
            bids=[(Decimal("50000"), Decimal("1.0"))],
            asks=[(Decimal("50001"), Decimal("0.5"))],
            timestamp=1700000000.0,
        )
        assert len(snap.bids) == 1
        assert snap.bids[0] == (Decimal("50000"), Decimal("1.0"))

    def test_empty_book(self) -> None:
        snap = OrderBookSnapshot(
            trading_pair="BTC-USDT",
            bids=[],
            asks=[],
            timestamp=0.0,
        )
        assert snap.bids == []

    def test_frozen(self) -> None:
        snap = OrderBookSnapshot(
            trading_pair="BTC-USDT",
            bids=[],
            asks=[],
            timestamp=0.0,
        )
        with pytest.raises(ValidationError):
            snap.timestamp = 999.0


class TestOrderBookUpdate:
    def test_create(self) -> None:
        update = OrderBookUpdate(
            trading_pair="BTC-USDT",
            bids=[(Decimal("49999"), Decimal("2.0"))],
            asks=[],
            update_id=42,
        )
        assert update.update_id == 42

    def test_frozen(self) -> None:
        update = OrderBookUpdate(
            trading_pair="BTC-USDT",
            bids=[],
            asks=[],
            update_id=1,
        )
        with pytest.raises(ValidationError):
            update.update_id = 99
