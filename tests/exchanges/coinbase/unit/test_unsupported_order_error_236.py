"""Coinbase raises UnsupportedOrderError (not ValueError) for unsupported order types (#236)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from data_type_primitives.common import OrderType as DtpOrderType

from market_connector.exceptions import GatewayError, UnsupportedOrderError
from market_connector.exchanges.coinbase.mixins.orders import OrdersMixin
from market_connector.primitives import OrderType, TradeType
from market_connector.transport.response import Response

_OK = {"success": True, "order_id": "o1", "success_response": {"order_id": "o1"}}
_AMOUNT = Decimal("0.5")
_PRICE = Decimal("50000")


class _Rest:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def request(self, endpoint_name: str, **_: Any) -> Response:
        self.calls.append(endpoint_name)
        return Response(raw=_OK, status_code=200, headers=httpx.Headers(), _endpoint=endpoint_name)


class _Orders(OrdersMixin):
    def __init__(self, rest: _Rest) -> None:
        self._rest = rest
        self._endpoints: dict[str, Any] = {}

    @property
    def ready(self) -> bool:
        return True


@pytest.fixture
def rest() -> _Rest:
    return _Rest()


@pytest.mark.parametrize(
    "order_type",
    [
        pytest.param(OrderType.STOP_LOSS, id="local-stop-loss"),
        pytest.param(DtpOrderType.STOP_LOSS, id="dtp-stop-loss"),
        pytest.param(DtpOrderType.TAKE_PROFIT, id="dtp-take-profit"),
        pytest.param("stop_loss", id="str-stop-loss"),
    ],
)
async def test_unsupported_type_raises_unsupported_order_error(
    rest: _Rest, order_type: Any
) -> None:
    with pytest.raises(UnsupportedOrderError) as exc:
        await _Orders(rest).place_order(
            "BTC-USD", order_type, TradeType.BUY, _AMOUNT, _PRICE, client_order_id="c1"
        )
    assert exc.value.exchange == "coinbase"
    assert exc.value.order_type is not None
    assert isinstance(exc.value, GatewayError)
    assert not isinstance(exc.value, ValueError)
    assert rest.calls == []
