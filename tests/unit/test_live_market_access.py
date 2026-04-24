"""Unit tests for LiveMarketAccess.

All hummingbot imports are isolated behind MagicMock — no hummingbot installation
required.  strategy-framework imports are optional: protocol-satisfaction tests are
skipped when the package is not installed.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from types import ModuleType
from unittest.mock import MagicMock

import pytest

from market_connector.live_market_access import LiveMarketAccess

TRADING_PAIR = "BTC-USDT"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_connector(
    mid_price: Decimal = Decimal("50000"),
    available_balance: Decimal = Decimal("1000"),
    buy_order_id: str = "buy-001",
    sell_order_id: str = "sell-001",
    quantized_amount: Decimal = Decimal("0.001"),
    quantized_price: Decimal = Decimal("50000"),
) -> MagicMock:
    """Return a MagicMock that mimics a ConnectorBase."""
    connector = MagicMock()
    connector.get_mid_price.return_value = mid_price
    connector.get_available_balance.return_value = available_balance
    connector.buy.return_value = buy_order_id
    connector.sell.return_value = sell_order_id
    connector.quantize_order_amount.return_value = quantized_amount
    connector.quantize_order_price.return_value = quantized_price
    return connector


def _make_hb_trading_rule(
    trading_pair: str = TRADING_PAIR,
    min_order_size: Decimal = Decimal("0.001"),
    max_order_size: Decimal = Decimal("1000"),
    min_price_increment: Decimal = Decimal("0.01"),
    min_base_amount_increment: Decimal = Decimal("0.00001"),
    min_notional_size: Decimal = Decimal("10"),
    supports_limit_orders: bool = True,
    supports_market_orders: bool = True,
) -> MagicMock:
    """Return a MagicMock that mimics a hummingbot TradingRule."""
    rule = MagicMock()
    rule.trading_pair = trading_pair
    rule.min_order_size = min_order_size
    rule.max_order_size = max_order_size
    rule.min_price_increment = min_price_increment
    rule.min_base_amount_increment = min_base_amount_increment
    rule.min_notional_size = min_notional_size
    rule.supports_limit_orders = supports_limit_orders
    rule.supports_market_orders = supports_market_orders
    return rule


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def connector() -> MagicMock:
    return _make_connector()


@pytest.fixture()
def market(connector: MagicMock) -> LiveMarketAccess:
    return LiveMarketAccess(connector, TRADING_PAIR)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_stores_connector_and_trading_pair(self, connector: MagicMock) -> None:
        market = LiveMarketAccess(connector, TRADING_PAIR)
        assert market._connector is connector
        assert market._trading_pair == TRADING_PAIR

    def test_accepts_any_connector_type(self) -> None:
        """connector is typed Any — accepts arbitrary objects."""
        market = LiveMarketAccess(object(), "ETH-USDT")
        assert market._trading_pair == "ETH-USDT"


# ---------------------------------------------------------------------------
# place_order
# ---------------------------------------------------------------------------

# We need to stub out the hummingbot enums imported inside place_order.
# Build minimal stub modules so we don't need a full hummingbot installation.


def _make_hb_stub() -> None:
    """Inject minimal hummingbot stubs into sys.modules."""
    import enum

    hb = ModuleType("hummingbot")
    hb_core = ModuleType("hummingbot.core")
    hb_dt = ModuleType("hummingbot.core.data_type")
    hb_common = ModuleType("hummingbot.core.data_type.common")

    # Use real Python enums so that EnumClass["KEY"] works as expected.
    class TradeType(enum.Enum):
        BUY = "buy"
        SELL = "sell"

    class OrderType(enum.Enum):
        LIMIT = "limit"
        MARKET = "market"

    hb_common.TradeType = TradeType
    hb_common.OrderType = OrderType

    sys.modules.setdefault("hummingbot", hb)
    sys.modules.setdefault("hummingbot.core", hb_core)
    sys.modules.setdefault("hummingbot.core.data_type", hb_dt)
    sys.modules.setdefault("hummingbot.core.data_type.common", hb_common)


_make_hb_stub()


class TestPlaceOrder:
    def test_buy_delegates_to_connector_buy(
        self, market: LiveMarketAccess, connector: MagicMock
    ) -> None:
        order_id = market.place_order("LIMIT", "BUY", Decimal("0.001"), Decimal("50000"))
        connector.buy.assert_called_once()
        call_args = connector.buy.call_args[0]
        assert call_args[0] == TRADING_PAIR
        assert call_args[1] == Decimal("0.001")
        assert call_args[3] == Decimal("50000")
        assert order_id == "buy-001"

    def test_sell_delegates_to_connector_sell(
        self, market: LiveMarketAccess, connector: MagicMock
    ) -> None:
        order_id = market.place_order("LIMIT", "SELL", Decimal("0.001"), Decimal("50000"))
        connector.sell.assert_called_once()
        call_args = connector.sell.call_args[0]
        assert call_args[0] == TRADING_PAIR
        assert order_id == "sell-001"

    def test_buy_is_case_insensitive(self, market: LiveMarketAccess, connector: MagicMock) -> None:
        market.place_order("limit", "buy", Decimal("0.001"), Decimal("50000"))
        connector.buy.assert_called_once()

    def test_sell_is_case_insensitive(self, market: LiveMarketAccess, connector: MagicMock) -> None:
        market.place_order("LIMIT", "sell", Decimal("0.001"), Decimal("50000"))
        connector.sell.assert_called_once()

    def test_returns_connector_order_id(
        self, market: LiveMarketAccess, connector: MagicMock
    ) -> None:
        connector.buy.return_value = "custom-id-123"
        result = market.place_order("LIMIT", "BUY", Decimal("1"), Decimal("100"))
        assert result == "custom-id-123"


# ---------------------------------------------------------------------------
# cancel_order
# ---------------------------------------------------------------------------


class TestCancelOrder:
    def test_delegates_to_connector_cancel(
        self, market: LiveMarketAccess, connector: MagicMock
    ) -> None:
        market.cancel_order("order-abc")
        connector.cancel.assert_called_once_with(TRADING_PAIR, "order-abc")

    def test_returns_none(self, market: LiveMarketAccess) -> None:
        result = market.cancel_order("order-xyz")
        assert result is None

    def test_passes_trading_pair_from_constructor(self, connector: MagicMock) -> None:
        market = LiveMarketAccess(connector, "ETH-USDT")
        market.cancel_order("oid-1")
        connector.cancel.assert_called_once_with("ETH-USDT", "oid-1")


# ---------------------------------------------------------------------------
# get_mid_price
# ---------------------------------------------------------------------------


class TestGetMidPrice:
    def test_delegates_with_bound_trading_pair(
        self, market: LiveMarketAccess, connector: MagicMock
    ) -> None:
        result = market.get_mid_price()
        connector.get_mid_price.assert_called_once_with(TRADING_PAIR)
        assert result == Decimal("50000")

    def test_uses_trading_pair_from_constructor(self, connector: MagicMock) -> None:
        connector.get_mid_price.return_value = Decimal("1800")
        market = LiveMarketAccess(connector, "ETH-USDT")
        market.get_mid_price()
        connector.get_mid_price.assert_called_once_with("ETH-USDT")

    def test_returns_decimal(self, market: LiveMarketAccess, connector: MagicMock) -> None:
        connector.get_mid_price.return_value = Decimal("12345.67")
        result = market.get_mid_price()
        assert isinstance(result, Decimal)
        assert result == Decimal("12345.67")


# ---------------------------------------------------------------------------
# get_available_balance
# ---------------------------------------------------------------------------


class TestGetAvailableBalance:
    def test_passes_currency_through(self, market: LiveMarketAccess, connector: MagicMock) -> None:
        result = market.get_available_balance("USDT")
        connector.get_available_balance.assert_called_once_with("USDT")
        assert result == Decimal("1000")

    def test_different_currencies(self, market: LiveMarketAccess, connector: MagicMock) -> None:
        connector.get_available_balance.return_value = Decimal("0.5")
        result = market.get_available_balance("BTC")
        connector.get_available_balance.assert_called_once_with("BTC")
        assert result == Decimal("0.5")


# ---------------------------------------------------------------------------
# get_trading_rules
# ---------------------------------------------------------------------------


class TestGetTradingRules:
    def _attach_rule(
        self, connector: MagicMock, trading_pair: str = TRADING_PAIR, **kwargs
    ) -> MagicMock:
        rule = _make_hb_trading_rule(trading_pair=trading_pair, **kwargs)
        connector.trading_rules = {trading_pair: rule}
        return rule

    def test_converts_hb_rule_to_framework_trading_rules(
        self, market: LiveMarketAccess, connector: MagicMock
    ) -> None:
        pytest.importorskip("strategy_framework")
        from strategy_framework.primitives.trading_rules import TradingRules

        self._attach_rule(connector)
        result = market.get_trading_rules(TRADING_PAIR)
        assert isinstance(result, TradingRules)

    def test_trading_pair_field(self, market: LiveMarketAccess, connector: MagicMock) -> None:
        pytest.importorskip("strategy_framework")
        self._attach_rule(connector)
        result = market.get_trading_rules(TRADING_PAIR)
        assert result.trading_pair == TRADING_PAIR

    def test_min_order_size_field(self, market: LiveMarketAccess, connector: MagicMock) -> None:
        pytest.importorskip("strategy_framework")
        self._attach_rule(connector, min_order_size=Decimal("0.0001"))
        result = market.get_trading_rules(TRADING_PAIR)
        assert result.min_order_size == Decimal("0.0001")

    def test_max_order_size_field(self, market: LiveMarketAccess, connector: MagicMock) -> None:
        pytest.importorskip("strategy_framework")
        self._attach_rule(connector, max_order_size=Decimal("500"))
        result = market.get_trading_rules(TRADING_PAIR)
        assert result.max_order_size == Decimal("500")

    def test_max_order_size_none(self, market: LiveMarketAccess, connector: MagicMock) -> None:
        pytest.importorskip("strategy_framework")
        rule = _make_hb_trading_rule()
        rule.max_order_size = None
        connector.trading_rules = {TRADING_PAIR: rule}
        result = market.get_trading_rules(TRADING_PAIR)
        assert result.max_order_size is None

    def test_min_price_increment_field(
        self, market: LiveMarketAccess, connector: MagicMock
    ) -> None:
        pytest.importorskip("strategy_framework")
        self._attach_rule(connector, min_price_increment=Decimal("0.5"))
        result = market.get_trading_rules(TRADING_PAIR)
        assert result.min_price_increment == Decimal("0.5")

    def test_min_base_amount_increment_field(
        self, market: LiveMarketAccess, connector: MagicMock
    ) -> None:
        pytest.importorskip("strategy_framework")
        self._attach_rule(connector, min_base_amount_increment=Decimal("0.00001"))
        result = market.get_trading_rules(TRADING_PAIR)
        assert result.min_base_amount_increment == Decimal("0.00001")

    def test_min_notional_size_field(self, market: LiveMarketAccess, connector: MagicMock) -> None:
        pytest.importorskip("strategy_framework")
        self._attach_rule(connector, min_notional_size=Decimal("5"))
        result = market.get_trading_rules(TRADING_PAIR)
        assert result.min_notional_size == Decimal("5")

    def test_supports_limit_orders_field(
        self, market: LiveMarketAccess, connector: MagicMock
    ) -> None:
        pytest.importorskip("strategy_framework")
        self._attach_rule(connector, supports_limit_orders=True)
        result = market.get_trading_rules(TRADING_PAIR)
        assert result.supports_limit_orders is True

    def test_supports_market_orders_false(
        self, market: LiveMarketAccess, connector: MagicMock
    ) -> None:
        pytest.importorskip("strategy_framework")
        self._attach_rule(connector, supports_market_orders=False)
        result = market.get_trading_rules(TRADING_PAIR)
        assert result.supports_market_orders is False

    def test_missing_pair_raises_key_error(
        self, market: LiveMarketAccess, connector: MagicMock
    ) -> None:
        pytest.importorskip("strategy_framework")
        connector.trading_rules = {}
        with pytest.raises(KeyError):
            market.get_trading_rules("UNKNOWN-PAIR")


# ---------------------------------------------------------------------------
# quantize methods
# ---------------------------------------------------------------------------


class TestQuantizeMethods:
    def test_quantize_order_amount_delegates(
        self, market: LiveMarketAccess, connector: MagicMock
    ) -> None:
        result = market.quantize_order_amount(TRADING_PAIR, Decimal("0.001234"))
        connector.quantize_order_amount.assert_called_once_with(TRADING_PAIR, Decimal("0.001234"))
        assert result == Decimal("0.001")

    def test_quantize_order_price_delegates(
        self, market: LiveMarketAccess, connector: MagicMock
    ) -> None:
        result = market.quantize_order_price(TRADING_PAIR, Decimal("50001.23"))
        connector.quantize_order_price.assert_called_once_with(TRADING_PAIR, Decimal("50001.23"))
        assert result == Decimal("50000")

    def test_quantize_amount_different_pair(self, connector: MagicMock) -> None:
        market = LiveMarketAccess(connector, "ETH-USDT")
        connector.quantize_order_amount.return_value = Decimal("0.01")
        result = market.quantize_order_amount("ETH-USDT", Decimal("0.0134"))
        connector.quantize_order_amount.assert_called_once_with("ETH-USDT", Decimal("0.0134"))
        assert result == Decimal("0.01")

    def test_quantize_price_different_pair(self, connector: MagicMock) -> None:
        market = LiveMarketAccess(connector, "ETH-USDT")
        connector.quantize_order_price.return_value = Decimal("1800")
        result = market.quantize_order_price("ETH-USDT", Decimal("1800.5"))
        connector.quantize_order_price.assert_called_once_with("ETH-USDT", Decimal("1800.5"))
        assert result == Decimal("1800")


# ---------------------------------------------------------------------------
# Protocol satisfaction (requires strategy-framework)
# ---------------------------------------------------------------------------


class TestProtocolSatisfaction:
    def test_satisfies_market_access_protocol(self, market: LiveMarketAccess) -> None:
        sf_market = pytest.importorskip("strategy_framework.protocols.market")
        protocol_cls = sf_market.MarketAccessProtocol
        assert isinstance(market, protocol_cls)

    def test_satisfies_trading_rules_protocol(self, market: LiveMarketAccess) -> None:
        sf_tr = pytest.importorskip("strategy_framework.protocols.trading_rules")
        protocol_cls = sf_tr.TradingRulesProtocol
        assert isinstance(market, protocol_cls)
