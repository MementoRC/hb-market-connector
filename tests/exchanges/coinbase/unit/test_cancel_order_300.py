"""Acceptance tests for #300: Coinbase cancel_order must send the exchange order id."""

from decimal import Decimal
from typing import Any

from market_connector.exchanges.coinbase.mixins.orders import OrdersMixin
from market_connector.primitives import OrderType, TradeType
from market_connector.testing.mock_transport import MockRestClient


class _RecordingRest(MockRestClient):
    """MockRestClient that records calls and serves list_orders pages by cursor."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, dict[str, Any] | None, dict[str, Any] | None]] = []
        self.pages: dict[str | None, dict[str, Any]] = {}

    async def request(self, endpoint_name, params=None, data=None, headers=None):
        self.calls.append((endpoint_name, params, data))
        if endpoint_name == "list_orders" and self.pages:
            self._responses["list_orders"] = self.pages[(params or {}).get("cursor")]
        return await super().request(endpoint_name, params=params, data=data, headers=headers)

    def cancel_bodies(self) -> list[dict[str, Any] | None]:
        return [d for name, _, d in self.calls if name == "cancel_orders"]


class _Gateway(OrdersMixin):
    def __init__(self, rest: _RecordingRest) -> None:
        self._rest = rest
        self._endpoints = {}
        self._started = True

    @property
    def ready(self) -> bool:
        return self._started


def _order(order_id: str, client_order_id: str) -> dict[str, Any]:
    return {
        "order_id": order_id,
        "client_order_id": client_order_id,
        "product_id": "BTC-USD",
        "side": "BUY",
        "status": "OPEN",
        "order_configuration": {"limit_limit_gtc": {"base_size": "0.5", "limit_price": "50000"}},
        "filled_size": "0",
        "average_filled_price": "0",
    }


def _cancel_result(order_id: str, *, success: bool = True) -> dict[str, Any]:
    return {
        "results": [
            {
                "success": success,
                "order_id": order_id,
                "failure_reason": None if success else "UNKNOWN_CANCEL_FAILURE_REASON",
            }
        ]
    }


async def _place(rest: _RecordingRest, gateway: _Gateway, exchange_id: str) -> str:
    rest.register(
        "place_order",
        {
            "success": True,
            "order_id": exchange_id,
            "success_response": {
                "order_id": exchange_id,
                "product_id": "BTC-USD",
                "side": "BUY",
                "client_order_id": "ignored",
            },
        },
    )
    return await gateway.place_order(
        "BTC-USD", OrderType.LIMIT, TradeType.BUY, Decimal("0.5"), Decimal("50000")
    )


async def test_cancel_after_place_sends_exchange_order_id():
    rest = _RecordingRest()
    gateway = _Gateway(rest)
    client_id = await _place(rest, gateway, "X")
    rest.register("cancel_orders", _cancel_result("X"))

    assert await gateway.cancel_order("BTC-USD", client_id) is True
    assert rest.cancel_bodies() == [{"order_ids": ["X"]}]


async def test_cancel_on_fresh_gateway_resolves_exchange_id_via_lookup():
    rest = _RecordingRest()
    rest.register("list_orders", {"orders": [_order("X", "C")]})
    rest.register("cancel_orders", _cancel_result("X"))
    gateway = _Gateway(rest)

    assert await gateway.cancel_order("BTC-USD", "C") is True
    assert rest.cancel_bodies() == [{"order_ids": ["X"]}]
    assert any(name == "list_orders" for name, _, _ in rest.calls)


async def test_cancel_on_fresh_gateway_resolves_across_two_pages():
    rest = _RecordingRest()
    rest.pages = {
        None: {"orders": [_order("Y", "other")], "has_next": True, "cursor": "p2"},
        "p2": {"orders": [_order("X", "C")], "has_next": False},
    }
    rest.register("cancel_orders", _cancel_result("X"))
    gateway = _Gateway(rest)

    assert await gateway.cancel_order("BTC-USD", "C") is True
    assert rest.cancel_bodies() == [{"order_ids": ["X"]}]


async def test_cancel_unresolvable_client_id_returns_false_and_sends_no_cancel():
    rest = _RecordingRest()
    rest.register("list_orders", {"orders": [_order("Y", "other")]})
    rest.register("cancel_orders", _cancel_result("Y"))
    gateway = _Gateway(rest)

    assert await gateway.cancel_order("BTC-USD", "C") is False
    assert rest.cancel_bodies() == []


async def test_cancel_reported_failure_returns_false():
    rest = _RecordingRest()
    gateway = _Gateway(rest)
    client_id = await _place(rest, gateway, "X")
    rest.register("cancel_orders", _cancel_result("X", success=False))

    assert await gateway.cancel_order("BTC-USD", client_id) is False
    assert rest.cancel_bodies() == [{"order_ids": ["X"]}]
