"""Coinbase client-order-id pre-assignment and lookup by client_order_id (#221), RED.

Contract (gateway / ``OrdersMixin`` level):
  * ``place_order(..., *, client_order_id: str | None = None)`` sends the given id in the POST
    body; generates ``coinbase-<uuid4>`` when omitted; returns the id used.
  * ``get_order_by_client_order_id(client_order_id)`` returns the matching ``OpenOrder`` or
    ``None``, via the order-listing endpoint (matched on each order's ``client_order_id``),
    never via POST /orders.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from market_connector.exchanges.coinbase.mixins.orders import OrdersMixin
from market_connector.primitives import OrderType, TradeType
from market_connector.testing.mock_transport import MockRestClient


class _RecordingRest(MockRestClient):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def request(self, endpoint_name: str, params=None, data=None, headers=None):
        self.calls.append((endpoint_name, dict(data or params or {})))
        return await super().request(endpoint_name, params, data, headers)

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


class _Gateway(OrdersMixin):
    def __init__(self, rest: _RecordingRest) -> None:
        self._rest = rest
        self._endpoints = {}

    @property
    def ready(self) -> bool:
        return True


def _order(order_id: str, client_order_id: str, status: str = "OPEN") -> dict[str, Any]:
    return {
        "order_id": order_id,
        "client_order_id": client_order_id,
        "product_id": "BTC-USD",
        "side": "BUY",
        "status": status,
        "order_configuration": {"limit_limit_gtc": {"base_size": "0.5", "limit_price": "50000"}},
        "filled_size": "0",
        "average_filled_price": "0",
    }


def _ok_reply(order_id: str = "o1", client_order_id: str = "ignored") -> dict[str, Any]:
    return {
        "success": True,
        "order_id": order_id,
        "success_response": {
            "order_id": order_id,
            "product_id": "BTC-USD",
            "side": "BUY",
            "client_order_id": client_order_id,
        },
    }


async def _place(gw: _Gateway, **kwargs: Any) -> str:
    return await gw.place_order(
        "BTC-USD", OrderType.LIMIT, TradeType.BUY, Decimal("0.5"), Decimal("50000"), **kwargs
    )


class TestClientOrderIdPreAssigned:
    async def test_supplied_client_order_id_is_sent_and_returned(self) -> None:
        rest = _RecordingRest()
        rest.register("place_order", _ok_reply())

        result = await _place(_Gateway(rest), client_order_id="my-cid-1")

        assert result == "my-cid-1"
        assert rest.calls[0][0] == "place_order"
        assert rest.calls[0][1]["client_order_id"] == "my-cid-1"

    async def test_generated_client_order_id_matches_the_body(self) -> None:
        rest = _RecordingRest()
        rest.register("place_order", _ok_reply())

        result = await _place(_Gateway(rest))

        assert result.startswith("coinbase-")
        assert rest.calls[0][1]["client_order_id"] == result

    async def test_success_still_sends_exactly_one_post(self) -> None:
        rest = _RecordingRest()
        rest.register("place_order", _ok_reply())

        await _place(_Gateway(rest))

        assert rest.names() == ["place_order"]


class TestGetOrderByClientOrderId:
    async def test_found_returns_exchange_id_client_id_and_status(self) -> None:
        rest = _RecordingRest()
        rest.register(
            "list_orders",
            {"orders": [_order("o-other", "c-other"), _order("o-42", "c-42", "FILLED")]},
        )

        order = await _Gateway(rest).get_order_by_client_order_id("c-42")

        assert order is not None
        assert order.exchange_order_id == "o-42"
        assert order.client_order_id == "c-42"
        assert order.status == "FILLED"

    async def test_absent_returns_none(self) -> None:
        rest = _RecordingRest()
        rest.register("list_orders", {"orders": [_order("o-other", "c-other")]})

        assert await _Gateway(rest).get_order_by_client_order_id("c-missing") is None

    async def test_empty_listing_returns_none(self) -> None:
        rest = _RecordingRest()
        rest.register("list_orders", {"orders": []})

        assert await _Gateway(rest).get_order_by_client_order_id("c-missing") is None

    @pytest.mark.parametrize("found", [True, False], ids=["found", "absent"])
    async def test_lookup_never_posts_an_order(self, found: bool) -> None:
        rest = _RecordingRest()
        rest.register("list_orders", {"orders": [_order("o-1", "c-1")] if found else []})

        await _Gateway(rest).get_order_by_client_order_id("c-1")

        assert rest.names()
        assert "place_order" not in rest.names()
        assert set(rest.names()) <= {"list_orders", "order_status"}
