"""RED acceptance tests for #243: dtp-typed ``place_order`` on both implementers.

Contract: ``place_order(order_type: OrderType | str, side: TradeType | str, amount, price)``
with dtp.common enums; inputs normalised by member name (coerce_*). Passing str still works but
emits DeprecationWarning; enums emit none; garbage raises ValueError.
"""

from __future__ import annotations

import asyncio
import threading
import warnings
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from data_type_primitives.common import OrderType, TradeType

from market_connector.hb_compat.bridge import LiveMarketAccess as Bridge
from market_connector.live_market_access import LiveMarketAccess as ConnectorAccess

if TYPE_CHECKING:
    from collections.abc import Iterator

PAIR = "BTC-USDT"
AMOUNT = Decimal("1")
PRICE = Decimal("50000")

ENUM_STR_CASES = [
    (OrderType.LIMIT, "LIMIT", TradeType.BUY, "BUY"),
    (OrderType.MARKET, "market", TradeType.SELL, "sell"),
    (OrderType.LIMIT, "Limit", TradeType.SELL, "Sell"),
]


@pytest.fixture
def loop() -> Iterator[asyncio.AbstractEventLoop]:
    lp = asyncio.new_event_loop()
    thread = threading.Thread(target=lp.run_forever, daemon=True)
    thread.start()
    yield lp
    lp.call_soon_threadsafe(lp.stop)
    thread.join(timeout=2)
    lp.close()


@pytest.fixture
def gateway() -> AsyncMock:
    gw = AsyncMock()
    gw.place_order = AsyncMock(return_value="order-1")
    return gw


@pytest.fixture
def bridge(gateway: AsyncMock, loop: asyncio.AbstractEventLoop) -> Bridge:
    return Bridge(gateway=gateway, trading_pair=PAIR, loop=loop)


@pytest.fixture
def connector() -> MagicMock:
    c = MagicMock()
    c.buy.return_value = "buy-1"
    c.sell.return_value = "sell-1"
    return c


@pytest.fixture
def access(connector: MagicMock) -> ConnectorAccess:
    return ConnectorAccess(connector, PAIR)


def _connector_call(connector: MagicMock) -> Any:
    calls = connector.buy.call_args_list + connector.sell.call_args_list
    assert len(calls) == 1
    return calls[0]


class TestBridgePlaceOrder:
    @pytest.mark.parametrize(("ot", "ot_s", "tt", "tt_s"), ENUM_STR_CASES)
    def test_enum_and_str_yield_identical_gateway_call(
        self, bridge: Bridge, gateway: AsyncMock, ot: Any, ot_s: str, tt: Any, tt_s: str
    ) -> None:
        bridge.place_order(ot, tt, AMOUNT, PRICE)
        enum_call = gateway.place_order.call_args
        gateway.place_order.reset_mock()
        with pytest.warns(DeprecationWarning):
            bridge.place_order(ot_s, tt_s, AMOUNT, PRICE)
        assert gateway.place_order.call_args == enum_call

    @pytest.mark.parametrize(("ot", "ot_s", "tt", "tt_s"), ENUM_STR_CASES)
    def test_gateway_receives_canonical_members(
        self, bridge: Bridge, gateway: AsyncMock, ot: Any, ot_s: str, tt: Any, tt_s: str
    ) -> None:
        bridge.place_order(ot, tt, AMOUNT, PRICE)
        _pair, got_ot, got_tt, got_amount, got_price = gateway.place_order.call_args.args
        assert _pair == PAIR
        assert isinstance(got_ot, OrderType)
        assert isinstance(got_tt, TradeType)
        assert got_ot is ot
        assert got_tt is tt
        assert (got_amount, got_price) == (AMOUNT, PRICE)

    def test_str_gateway_receives_canonical_members(
        self, bridge: Bridge, gateway: AsyncMock
    ) -> None:
        with pytest.warns(DeprecationWarning):
            bridge.place_order("limit", "buy", AMOUNT, PRICE)
        _pair, got_ot, got_tt, _a, _p = gateway.place_order.call_args.args
        assert got_ot is OrderType.LIMIT
        assert got_tt is TradeType.BUY

    def test_str_emits_deprecation_warning(self, bridge: Bridge) -> None:
        with pytest.warns(DeprecationWarning):
            assert bridge.place_order("LIMIT", "BUY", AMOUNT, PRICE) == "order-1"

    def test_enum_emits_no_deprecation_warning(self, bridge: Bridge) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            assert bridge.place_order(OrderType.LIMIT, TradeType.BUY, AMOUNT, PRICE) == "order-1"

    def test_garbage_order_type_raises(self, bridge: Bridge) -> None:
        with pytest.raises(ValueError), warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            bridge.place_order("bogus", TradeType.BUY, AMOUNT, PRICE)

    def test_garbage_side_raises(self, bridge: Bridge) -> None:
        with pytest.raises(ValueError), warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            bridge.place_order(OrderType.LIMIT, "bogus", AMOUNT, PRICE)


class TestConnectorAccessPlaceOrder:
    @pytest.mark.parametrize(("ot", "ot_s", "tt", "tt_s"), ENUM_STR_CASES)
    def test_enum_and_str_yield_identical_connector_call(
        self,
        access: ConnectorAccess,
        connector: MagicMock,
        ot: Any,
        ot_s: str,
        tt: Any,
        tt_s: str,
    ) -> None:
        access.place_order(ot, tt, AMOUNT, PRICE)
        enum_call = _connector_call(connector)
        connector.reset_mock()
        with pytest.warns(DeprecationWarning):
            access.place_order(ot_s, tt_s, AMOUNT, PRICE)
        assert _connector_call(connector) == enum_call

    def test_enum_buy_routes_to_buy(self, access: ConnectorAccess, connector: MagicMock) -> None:
        assert access.place_order(OrderType.LIMIT, TradeType.BUY, AMOUNT, PRICE) == "buy-1"
        connector.buy.assert_called_once()
        connector.sell.assert_not_called()

    def test_enum_sell_routes_to_sell(self, access: ConnectorAccess, connector: MagicMock) -> None:
        assert access.place_order(OrderType.MARKET, TradeType.SELL, AMOUNT, PRICE) == "sell-1"
        connector.sell.assert_called_once()
        connector.buy.assert_not_called()

    def test_str_emits_deprecation_warning(self, access: ConnectorAccess) -> None:
        with pytest.warns(DeprecationWarning):
            assert access.place_order("LIMIT", "BUY", AMOUNT, PRICE) == "buy-1"

    def test_enum_emits_no_deprecation_warning(self, access: ConnectorAccess) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            assert access.place_order(OrderType.LIMIT, TradeType.BUY, AMOUNT, PRICE) == "buy-1"

    def test_garbage_order_type_raises(self, access: ConnectorAccess) -> None:
        with pytest.raises(ValueError), warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            access.place_order("bogus", TradeType.BUY, AMOUNT, PRICE)

    def test_garbage_side_raises(self, access: ConnectorAccess) -> None:
        with pytest.raises(ValueError), warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            access.place_order(OrderType.LIMIT, "bogus", AMOUNT, PRICE)
