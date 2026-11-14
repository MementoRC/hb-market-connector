"""Kraken conditional orders (issue #209): STOP_LOSS / TAKE_PROFIT and *_LIMIT variants.

Only the REST transport is faked (it records the AddOrder request body); the
bridge -> converter -> mixin -> request-body path runs for real.

The consumer contract is ``data_type_primitives.common.OrderType`` (what the parent
hummingbot passes). market-connector's own narrower mirror is also checked by name.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from data_type_primitives.common import OrderType

from market_connector.exchanges.kraken.hb_compat.kraken_bridge import KrakenConnectorBridge
from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway
from market_connector.primitives import OrderType as LocalOrderType
from market_connector.primitives import TradeType
from market_connector.transport.response import Response

_ADD_ORDER_OK = {
    "error": [],
    "result": {"descr": {"order": "x"}, "txid": ["OCOND-XXXXX-YYYYYY"]},
}


class _RecordingRest:
    """Fake REST transport recording every request's endpoint and body."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def request(
        self, endpoint_name: str, data: dict[str, Any] | None = None, **_: Any
    ) -> Any:
        self.calls.append((endpoint_name, dict(data or {})))
        return Response(
            raw=_ADD_ORDER_OK,
            status_code=200,
            headers=httpx.Headers(),
            _endpoint=endpoint_name,
        )

    @property
    def bodies(self) -> list[dict[str, Any]]:
        return [body for name, body in self.calls if name == "add_order"]


@pytest.fixture
def rest() -> _RecordingRest:
    return _RecordingRest()


@pytest.fixture
def gateway(rest: _RecordingRest) -> KrakenGateway:
    gw = KrakenGateway(api_key="k", secret_key="c2VjcmV0")
    gw._rest = rest  # type: ignore[assignment]
    gw._started = True
    return gw


@pytest.fixture
def bridge(gateway: KrakenGateway) -> KrakenConnectorBridge:
    b = KrakenConnectorBridge(api_key="k", secret_key="c2VjcmV0")
    b._gateway = gateway
    return b


_PAIR = "XBTUSD"
_AMOUNT = Decimal("0.5")

_TRIGGER_ONLY = [
    pytest.param(OrderType.STOP_LOSS, "stop-loss", id="stop_loss"),
    pytest.param(OrderType.TAKE_PROFIT, "take-profit", id="take_profit"),
]
_WITH_LIMIT = [
    pytest.param(OrderType.STOP_LOSS_LIMIT, "stop-loss-limit", id="stop_loss_limit"),
    pytest.param(OrderType.TAKE_PROFIT_LIMIT, "take-profit-limit", id="take_profit_limit"),
]
# market_connector's local mirror, where the member exists (accepted by name too).
_LOCAL_TRIGGER_ONLY = [pytest.param(LocalOrderType.STOP_LOSS, "stop-loss", id="local_stop_loss")]
_LOCAL_WITH_LIMIT = [
    pytest.param(LocalOrderType.STOP_LOSS_LIMIT, "stop-loss-limit", id="local_stop_loss_limit")
]
_DEFERRED = [
    pytest.param(OrderType.TRAILING_STOP, id="trailing_stop"),
    pytest.param(OrderType.TRAILING_STOP_LIMIT, id="trailing_stop_limit"),
]


# ---------------------------------------------------------------------------
# Mixin / gateway level
# ---------------------------------------------------------------------------


class TestGatewayBodies:
    @pytest.mark.parametrize(("order_type", "expected"), _TRIGGER_ONLY + _LOCAL_TRIGGER_ONLY)
    async def test_trigger_only(
        self, gateway: KrakenGateway, rest: _RecordingRest, order_type: Any, expected: str
    ) -> None:
        txid = await gateway.place_order(
            _PAIR, order_type, TradeType.SELL, _AMOUNT, Decimal("27000.5")
        )
        assert txid == "OCOND-XXXXX-YYYYYY"
        (body,) = rest.bodies
        assert body["ordertype"] == expected
        assert body["price"] == "27000.5"
        assert "price2" not in body
        assert body["pair"] == _PAIR
        assert body["type"] == "sell"
        assert body["volume"] == "0.5"

    @pytest.mark.parametrize(("order_type", "expected"), _WITH_LIMIT + _LOCAL_WITH_LIMIT)
    async def test_trigger_plus_limit(
        self, gateway: KrakenGateway, rest: _RecordingRest, order_type: Any, expected: str
    ) -> None:
        await gateway.place_order(
            _PAIR,
            order_type,
            TradeType.SELL,
            _AMOUNT,
            Decimal("27000"),
            limit_price=Decimal("26950.5"),
        )
        (body,) = rest.bodies
        assert body["ordertype"] == expected
        assert body["price"] == "27000"
        assert body["price2"] == "26950.5"

    @pytest.mark.parametrize(("order_type", "expected"), _TRIGGER_ONLY)
    async def test_percent_trigger(
        self, gateway: KrakenGateway, rest: _RecordingRest, order_type: Any, expected: str
    ) -> None:
        await gateway.place_order(
            _PAIR, order_type, TradeType.SELL, _AMOUNT, Decimal("2.5"), price_in_percent=True
        )
        (body,) = rest.bodies
        assert body["ordertype"] == expected
        assert body["price"] == "#2.5%"
        assert "price2" not in body

    @pytest.mark.parametrize(("order_type", "expected"), _WITH_LIMIT)
    async def test_percent_trigger_with_limit(
        self, gateway: KrakenGateway, rest: _RecordingRest, order_type: Any, expected: str
    ) -> None:
        await gateway.place_order(
            _PAIR,
            order_type,
            TradeType.SELL,
            _AMOUNT,
            Decimal("2.5"),
            limit_price=Decimal("26950"),
            price_in_percent=True,
        )
        (body,) = rest.bodies
        assert body["ordertype"] == expected
        assert body["price"] == "#2.5%"
        assert body["price2"] == "26950"

    @pytest.mark.parametrize("order_type", _DEFERRED)
    async def test_trailing_deferred(
        self, gateway: KrakenGateway, rest: _RecordingRest, order_type: Any
    ) -> None:
        with pytest.raises(ValueError):  # noqa: PT011
            await gateway.place_order(_PAIR, order_type, TradeType.SELL, _AMOUNT, Decimal("100"))
        assert rest.calls == []


class TestRegression:
    @pytest.mark.parametrize("order_type", [OrderType.MARKET, LocalOrderType.MARKET])
    async def test_market_body_unchanged(
        self, gateway: KrakenGateway, rest: _RecordingRest, order_type: Any
    ) -> None:
        await gateway.place_order(_PAIR, order_type, TradeType.BUY, _AMOUNT)
        assert rest.bodies == [
            {"pair": _PAIR, "type": "buy", "ordertype": "market", "volume": "0.5"}
        ]

    @pytest.mark.parametrize("order_type", [OrderType.LIMIT, LocalOrderType.LIMIT])
    async def test_limit_body_unchanged(
        self, gateway: KrakenGateway, rest: _RecordingRest, order_type: Any
    ) -> None:
        await gateway.place_order(_PAIR, order_type, TradeType.SELL, _AMOUNT, Decimal("30000.0"))
        assert rest.bodies == [
            {
                "pair": _PAIR,
                "type": "sell",
                "ordertype": "limit",
                "volume": "0.5",
                "price": "30000.0",
            }
        ]

    @pytest.mark.parametrize("market", [OrderType.MARKET, LocalOrderType.MARKET])
    async def test_bridge_market_and_limit_unchanged(
        self, bridge: KrakenConnectorBridge, rest: _RecordingRest, market: Any
    ) -> None:
        await bridge.place_order(_PAIR, market, TradeType.BUY, _AMOUNT)
        await bridge.place_order(_PAIR, OrderType.LIMIT, TradeType.SELL, _AMOUNT, Decimal("30000"))
        assert rest.bodies == [
            {"pair": _PAIR, "type": "buy", "ordertype": "market", "volume": "0.5"},
            {
                "pair": _PAIR,
                "type": "sell",
                "ordertype": "limit",
                "volume": "0.5",
                "price": "30000",
            },
        ]


# ---------------------------------------------------------------------------
# Bridge end-to-end (bridge -> converter -> real mixin -> recording transport)
# ---------------------------------------------------------------------------


class TestBridgeEndToEnd:
    @pytest.mark.parametrize(("order_type", "expected"), _TRIGGER_ONLY + _LOCAL_TRIGGER_ONLY)
    async def test_trigger_only(
        self,
        bridge: KrakenConnectorBridge,
        rest: _RecordingRest,
        order_type: Any,
        expected: str,
    ) -> None:
        txid = await bridge.place_order(
            _PAIR, order_type, TradeType.SELL, _AMOUNT, Decimal("27000.5")
        )
        assert txid == "OCOND-XXXXX-YYYYYY"
        (body,) = rest.bodies
        assert body["ordertype"] == expected
        assert body["price"] == "27000.5"
        assert "price2" not in body

    @pytest.mark.parametrize(("order_type", "expected"), _WITH_LIMIT + _LOCAL_WITH_LIMIT)
    async def test_trigger_plus_limit(
        self,
        bridge: KrakenConnectorBridge,
        rest: _RecordingRest,
        order_type: Any,
        expected: str,
    ) -> None:
        await bridge.place_order(
            _PAIR,
            order_type,
            TradeType.SELL,
            _AMOUNT,
            Decimal("27000"),
            limit_price=Decimal("26950"),
        )
        (body,) = rest.bodies
        assert body["ordertype"] == expected
        assert body["price"] == "27000"
        assert body["price2"] == "26950"

    async def test_percent_trigger(
        self, bridge: KrakenConnectorBridge, rest: _RecordingRest
    ) -> None:
        await bridge.place_order(
            _PAIR,
            OrderType.STOP_LOSS,
            TradeType.SELL,
            _AMOUNT,
            Decimal("2.5"),
            price_in_percent=True,
        )
        (body,) = rest.bodies
        assert body["ordertype"] == "stop-loss"
        assert body["price"] == "#2.5%"


# ---------------------------------------------------------------------------
# Error rules: ValueError before any HTTP call (gateway and bridge)
# ---------------------------------------------------------------------------

_P = Decimal("27000")
_L = Decimal("26950")
_BAD_ARGS = [
    pytest.param(OrderType.STOP_LOSS, None, None, False, id="stop_loss_no_trigger"),
    pytest.param(OrderType.TAKE_PROFIT, None, None, False, id="take_profit_no_trigger"),
    pytest.param(OrderType.STOP_LOSS_LIMIT, None, _L, False, id="sll_no_trigger"),
    pytest.param(OrderType.TAKE_PROFIT_LIMIT, None, _L, False, id="tpl_no_trigger"),
    pytest.param(OrderType.STOP_LOSS_LIMIT, _P, None, False, id="sll_no_limit_price"),
    pytest.param(OrderType.TAKE_PROFIT_LIMIT, _P, None, False, id="tpl_no_limit_price"),
    pytest.param(OrderType.STOP_LOSS, _P, _L, False, id="sl_with_limit_price"),
    pytest.param(OrderType.TAKE_PROFIT, _P, _L, False, id="tp_with_limit_price"),
    pytest.param(OrderType.LIMIT, _P, _L, False, id="limit_with_limit_price"),
    pytest.param(OrderType.MARKET, None, _L, False, id="market_with_limit_price"),
    pytest.param(OrderType.MARKET, None, None, True, id="market_percent"),
    pytest.param(OrderType.LIMIT, Decimal("2.5"), None, True, id="limit_percent"),
    pytest.param(OrderType.TRAILING_STOP, _P, None, False, id="trailing_stop_deferred"),
    pytest.param(OrderType.TRAILING_STOP_LIMIT, _P, _L, False, id="trailing_stop_limit_deferred"),
]


class TestErrorRules:
    @pytest.mark.parametrize(("order_type", "price", "limit_price", "percent"), _BAD_ARGS)
    async def test_gateway_rejects_before_http(
        self,
        gateway: KrakenGateway,
        rest: _RecordingRest,
        order_type: Any,
        price: Decimal | None,
        limit_price: Decimal | None,
        percent: bool,
    ) -> None:
        with pytest.raises(ValueError):  # noqa: PT011
            await gateway.place_order(
                _PAIR,
                order_type,
                TradeType.SELL,
                _AMOUNT,
                price,
                limit_price=limit_price,
                price_in_percent=percent,
            )
        assert rest.calls == []

    @pytest.mark.parametrize(("order_type", "price", "limit_price", "percent"), _BAD_ARGS)
    async def test_bridge_rejects_before_http(
        self,
        bridge: KrakenConnectorBridge,
        rest: _RecordingRest,
        order_type: Any,
        price: Decimal | None,
        limit_price: Decimal | None,
        percent: bool,
    ) -> None:
        with pytest.raises(ValueError):  # noqa: PT011
            await bridge.place_order(
                _PAIR,
                order_type,
                TradeType.SELL,
                _AMOUNT,
                price,
                limit_price=limit_price,
                price_in_percent=percent,
            )
        assert rest.calls == []
