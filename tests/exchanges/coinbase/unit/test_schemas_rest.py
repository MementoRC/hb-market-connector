"""Tests for Coinbase REST response schema models."""

import json
from pathlib import Path

import pytest

from market_connector.exchanges.coinbase.schemas.rest import (
    Account,
    Balance,
    CancelOrdersResponse,
    Candle,
    CreateOrderResponse,
    Fill,
    GetProductCandlesResponse,
    ListAccountsResponse,
    ListFillsResponse,
    ListOrdersResponse,
    ListProductsResponse,
    Order,
    OrderBookLevel,
    OrderBookResponse,
    Product,
    ServerTimeResponse,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "rest"


class TestBalance:
    def test_balance_fields(self):
        b = Balance.model_validate({"value": "0.5", "currency": "BTC"})
        assert b.value == "0.5"
        assert b.currency == "BTC"

    def test_balance_is_frozen(self):
        from pydantic import ValidationError  # noqa: PLC0415

        b = Balance.model_validate({"value": "1.0", "currency": "USD"})
        with pytest.raises(ValidationError):
            b.value = "2.0"  # type: ignore[misc]


class TestAccount:
    def test_account_parses_fixture(self):
        data = json.loads((FIXTURES / "account.json").read_text())
        account = Account.model_validate(data)
        assert account.currency == "BTC"
        assert account.available_balance.value == "0.5"
        assert account.available_balance.currency == "BTC"
        assert account.hold.value == "0.1"
        assert account.default is True
        assert account.active is True

    def test_account_optional_type(self):
        data = json.loads((FIXTURES / "account.json").read_text())
        account = Account.model_validate(data)
        assert account.type == "ACCOUNT_TYPE_CRYPTO"

    def test_account_ignores_extra_fields(self):
        data = {
            "uuid": "abc",
            "name": "Wallet",
            "currency": "ETH",
            "available_balance": {"value": "1.0", "currency": "ETH"},
            "hold": {"value": "0.0", "currency": "ETH"},
            "extra_unknown_field": "ignored",
        }
        account = Account.model_validate(data)
        assert account.currency == "ETH"


class TestListAccountsResponse:
    def test_list_accounts_response(self):
        data = {
            "accounts": [
                {
                    "uuid": "abc",
                    "name": "BTC Wallet",
                    "currency": "BTC",
                    "available_balance": {"value": "1.0", "currency": "BTC"},
                    "hold": {"value": "0.0", "currency": "BTC"},
                }
            ],
            "has_next": False,
            "cursor": None,
            "size": 1,
        }
        resp = ListAccountsResponse.model_validate(data)
        assert len(resp.accounts) == 1
        assert resp.accounts[0].currency == "BTC"
        assert resp.has_next is False


class TestProduct:
    def test_product_parses_fixture(self):
        data = json.loads((FIXTURES / "product.json").read_text())
        product = Product.model_validate(data)
        assert product.product_id == "BTC-USD"
        assert product.base_currency_id == "BTC"
        assert product.quote_currency_id == "USD"
        assert product.base_increment == "0.00000001"
        assert product.trading_disabled is False

    def test_product_optional_fields(self):
        data = json.loads((FIXTURES / "product.json").read_text())
        product = Product.model_validate(data)
        assert product.price == "50000.00"
        assert product.volume_24h == "12345.67"


class TestListProductsResponse:
    def test_list_products_response(self):
        data = json.loads((FIXTURES / "product.json").read_text())
        resp = ListProductsResponse.model_validate({"products": [data], "num_products": 1})
        assert len(resp.products) == 1
        assert resp.num_products == 1


class TestOrder:
    def test_order_parses_fixture(self):
        data = json.loads((FIXTURES / "order.json").read_text())
        order = Order.model_validate(data)
        assert order.order_id == "order-001"
        assert order.product_id == "BTC-USD"
        assert order.side == "BUY"
        assert order.status == "OPEN"

    def test_order_configuration_parsed(self):
        data = json.loads((FIXTURES / "order.json").read_text())
        order = Order.model_validate(data)
        assert order.order_configuration is not None
        cfg = order.order_configuration.limit_limit_gtc
        assert cfg is not None
        assert cfg.base_size == "0.01"
        assert cfg.limit_price == "50000.00"

    def test_order_enum_fields(self):
        from market_connector.exchanges.coinbase.schemas.enums import (
            CoinbaseOrderSide,
            CoinbaseOrderStatus,
        )

        data = json.loads((FIXTURES / "order.json").read_text())
        order = Order.model_validate(data)
        assert order.side == CoinbaseOrderSide.BUY
        assert order.status == CoinbaseOrderStatus.OPEN


class TestCreateOrderResponse:
    def test_success_response(self):
        data = {
            "success": True,
            "order_id": "order-001",
            "success_response": {
                "order_id": "order-001",
                "product_id": "BTC-USD",
                "side": "BUY",
                "client_order_id": "client-001",
            },
        }
        resp = CreateOrderResponse.model_validate(data)
        assert resp.success is True
        assert resp.success_response is not None
        assert resp.success_response.product_id == "BTC-USD"

    def test_failure_response(self):
        data = {"success": False, "failure_reason": "INSUFFICIENT_FUND"}
        resp = CreateOrderResponse.model_validate(data)
        assert resp.success is False
        assert resp.failure_reason == "INSUFFICIENT_FUND"
        assert resp.success_response is None


class TestCancelOrdersResponse:
    def test_cancel_results(self):
        data = {
            "results": [
                {"success": True, "order_id": "order-001"},
                {"success": False, "failure_reason": "NOT_FOUND", "order_id": "order-002"},
            ]
        }
        resp = CancelOrdersResponse.model_validate(data)
        assert len(resp.results) == 2
        assert resp.results[0].success is True
        assert resp.results[1].failure_reason == "NOT_FOUND"


class TestListOrdersResponse:
    def test_list_orders(self):
        data = json.loads((FIXTURES / "order.json").read_text())
        resp = ListOrdersResponse.model_validate({"orders": [data], "has_next": False})
        assert len(resp.orders) == 1
        assert resp.has_next is False


class TestFill:
    def test_fill_fields(self):
        data = {
            "entry_id": "fill-001",
            "trade_id": "trade-001",
            "order_id": "order-001",
            "trade_time": "2026-04-24T12:00:00Z",
            "trade_type": "FILL",
            "price": "50000.00",
            "size": "0.01",
            "commission": "0.05",
            "product_id": "BTC-USD",
            "side": "BUY",
        }
        fill = Fill.model_validate(data)
        assert fill.entry_id == "fill-001"
        assert fill.price == "50000.00"
        assert fill.side == "BUY"


class TestListFillsResponse:
    def test_list_fills(self):
        fill_data = {
            "entry_id": "fill-001",
            "trade_id": "t1",
            "order_id": "o1",
            "trade_time": "2026-04-24T12:00:00Z",
            "trade_type": "FILL",
            "price": "50000.00",
            "size": "0.01",
            "commission": "0.05",
            "product_id": "BTC-USD",
            "side": "BUY",
        }
        resp = ListFillsResponse.model_validate({"fills": [fill_data]})
        assert len(resp.fills) == 1


class TestCandle:
    def test_candle_fields(self):
        data = {
            "start": "1714003200",
            "low": "49800.00",
            "high": "50200.00",
            "open": "50000.00",
            "close": "50100.00",
            "volume": "123.45",
        }
        candle = Candle.model_validate(data)
        assert candle.start == "1714003200"
        assert candle.low == "49800.00"
        assert candle.volume == "123.45"


class TestGetProductCandlesResponse:
    def test_candles_fixture(self):
        data = json.loads((FIXTURES / "candles.json").read_text())
        resp = GetProductCandlesResponse.model_validate(data)
        assert len(resp.candles) == 2
        assert resp.candles[0].open == "50000.00"


class TestOrderBook:
    def test_orderbook_fixture(self):
        data = json.loads((FIXTURES / "orderbook.json").read_text())
        resp = OrderBookResponse.model_validate(data)
        assert resp.pricebook.product_id == "BTC-USD"
        assert len(resp.pricebook.bids) == 2
        assert len(resp.pricebook.asks) == 2
        assert resp.pricebook.bids[0].price == "49999.00"

    def test_orderbook_level(self):
        level = OrderBookLevel.model_validate({"price": "100.00", "size": "0.5"})
        assert level.price == "100.00"
        assert level.size == "0.5"


class TestServerTimeResponse:
    def test_server_time(self):
        data = {
            "iso": "2026-04-24T12:00:00Z",
            "epochSeconds": "1714003200",
            "epochMillis": "1714003200000",
        }
        resp = ServerTimeResponse.model_validate(data)
        assert resp.iso == "2026-04-24T12:00:00Z"
        assert resp.epoch_seconds == "1714003200"
        assert resp.epoch_millis == "1714003200000"
