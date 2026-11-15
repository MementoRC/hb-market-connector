"""Coinbase place_order accepts canonical dtp OrderType/TradeType, str, and local enums (#231).

Contract: ``order_type`` and ``side`` are normalised by member NAME (case-insensitive), so a
``data_type_primitives.common`` member, a string, or the local StrEnum all produce the SAME
request payload. Valid-but-unsupported types raise ``UnsupportedOrderError`` and unknown strings
raise ``ValueError("Unsupported order type ...")``, both before any HTTP call.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from data_type_primitives.common import OrderType as DtpOrderType
from data_type_primitives.common import TradeType as DtpTradeType

from market_connector.exceptions import UnsupportedOrderError
from market_connector.exchanges.coinbase.mixins.orders import OrdersMixin
from market_connector.primitives import OrderType, TradeType
from market_connector.transport.response import Response

_OK = {
    "success": True,
    "order_id": "o1",
    "success_response": {
        "order_id": "o1",
        "product_id": "BTC-USD",
        "side": "BUY",
        "client_order_id": "c1",
    },
}
_AMOUNT = Decimal("0.5")
_PRICE = Decimal("50000")


class _RecordingRest:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def request(
        self, endpoint_name: str, data: dict[str, Any] | None = None, **_: Any
    ) -> Response:
        self.calls.append((endpoint_name, dict(data or {})))
        return Response(raw=_OK, status_code=200, headers=httpx.Headers(), _endpoint=endpoint_name)


class _Orders(OrdersMixin):
    def __init__(self, rest: _RecordingRest) -> None:
        self._rest = rest
        self._endpoints: dict[str, Any] = {}

    @property
    def ready(self) -> bool:
        return True


@pytest.fixture
def rest() -> _RecordingRest:
    return _RecordingRest()


@pytest.fixture
def orders(rest: _RecordingRest) -> _Orders:
    return _Orders(rest)


async def _place(
    orders: _Orders, rest: _RecordingRest, order_type: Any, side: Any = TradeType.BUY
) -> dict[str, Any]:
    price = None if str(getattr(order_type, "name", order_type)).upper() == "MARKET" else _PRICE
    await orders.place_order("BTC-USD", order_type, side, _AMOUNT, price, client_order_id="c1")
    (body,) = [b for n, b in rest.calls if n == "place_order"]
    return body


_SUPPORTED = ["LIMIT", "MARKET", "LIMIT_MAKER"]


class TestSupportedTypesSamePayload:
    @pytest.mark.parametrize("name", _SUPPORTED)
    async def test_dtp_member_matches_local_enum(
        self, orders: _Orders, rest: _RecordingRest, name: str
    ) -> None:
        expected = await _place(orders, rest, OrderType[name])
        rest.calls.clear()
        assert await _place(orders, rest, DtpOrderType[name]) == expected

    @pytest.mark.parametrize("name", _SUPPORTED)
    @pytest.mark.parametrize("case", [str.lower, str.upper, str.title])
    async def test_string_matches_local_enum(
        self, orders: _Orders, rest: _RecordingRest, name: str, case: Any
    ) -> None:
        expected = await _place(orders, rest, OrderType[name])
        rest.calls.clear()
        assert await _place(orders, rest, case(name)) == expected

    async def test_limit_payload_shape(self, orders: _Orders, rest: _RecordingRest) -> None:
        body = await _place(orders, rest, DtpOrderType.LIMIT)
        assert body["order_configuration"] == {
            "limit_limit_gtc": {"base_size": "0.5", "limit_price": "50000", "post_only": False}
        }

    async def test_limit_maker_is_post_only(self, orders: _Orders, rest: _RecordingRest) -> None:
        body = await _place(orders, rest, DtpOrderType.LIMIT_MAKER)
        assert body["order_configuration"]["limit_limit_gtc"]["post_only"] is True

    async def test_market_payload_shape(self, orders: _Orders, rest: _RecordingRest) -> None:
        body = await _place(orders, rest, DtpOrderType.MARKET)
        assert body["order_configuration"] == {"market_market_ioc": {"base_size": "0.5"}}


class TestSide:
    @pytest.mark.parametrize(
        ("side", "wire"),
        [
            pytest.param(DtpTradeType.BUY, "BUY", id="dtp-buy"),
            pytest.param(DtpTradeType.SELL, "SELL", id="dtp-sell"),
            pytest.param(TradeType.BUY, "BUY", id="local-buy"),
            pytest.param(TradeType.SELL, "SELL", id="local-sell"),
            pytest.param("BUY", "BUY", id="str-upper"),
            pytest.param("sell", "SELL", id="str-lower"),
        ],
    )
    async def test_side_normalised_to_wire(
        self, orders: _Orders, rest: _RecordingRest, side: Any, wire: str
    ) -> None:
        body = await _place(orders, rest, OrderType.LIMIT, side)
        assert body["side"] == wire


class TestUnsupportedTypes:
    @pytest.mark.parametrize(
        "order_type",
        [
            pytest.param(OrderType.STOP_LOSS, id="local-stop-loss"),
            pytest.param(DtpOrderType.STOP_LOSS, id="dtp-stop-loss"),
            pytest.param(DtpOrderType.STOP_LOSS_LIMIT, id="dtp-stop-loss-limit"),
            pytest.param(DtpOrderType.TAKE_PROFIT, id="dtp-take-profit"),
            pytest.param(DtpOrderType.TRAILING_STOP, id="dtp-trailing-stop"),
            pytest.param("stop_loss", id="str-stop-loss"),
        ],
    )
    async def test_raises_unsupported_before_http(
        self, orders: _Orders, rest: _RecordingRest, order_type: Any
    ) -> None:
        with pytest.raises(UnsupportedOrderError):
            await orders.place_order(
                "BTC-USD", order_type, TradeType.BUY, _AMOUNT, _PRICE, client_order_id="c1"
            )
        assert rest.calls == []

    async def test_garbage_string_raises_before_http(
        self, orders: _Orders, rest: _RecordingRest
    ) -> None:
        with pytest.raises(ValueError, match="Unsupported order type"):
            await orders.place_order(
                "BTC-USD", "bogus", TradeType.BUY, _AMOUNT, _PRICE, client_order_id="c1"
            )
        assert rest.calls == []
