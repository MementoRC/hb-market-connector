"""RED acceptance tests for #301 (Coinbase): paginated ``get_open_orders`` and queued statuses.

Goals pinned (behaviour, not implementation):

* ``get_open_orders`` follows ``cursor`` until ``has_next`` is false and returns the pair's
  OPEN orders from every page.
* ``CoinbaseOrderStatus`` recognises QUEUED, CANCEL_QUEUED and EDIT_QUEUED; a truly unknown
  string still falls back to UNKNOWN.
* QUEUED -> PENDING_CREATE, CANCEL_QUEUED -> PENDING_CANCEL, EDIT_QUEUED -> OPEN.
* A QUEUED leg looked up via ``get_leg`` reports PENDING_CREATE.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from data_type_primitives import OrderState

from market_connector.exchanges.coinbase.coinbase_gateway import CoinbaseGateway
from market_connector.exchanges.coinbase.config import CoinbaseConfig
from market_connector.exchanges.coinbase.converters import to_open_order
from market_connector.exchanges.coinbase.order_state import to_order_state
from market_connector.exchanges.coinbase.schemas.enums import CoinbaseOrderStatus
from market_connector.exchanges.coinbase.schemas.rest import Order
from market_connector.transport.response import Response

_PAIR = "BTC-USD"
_PAGE = 2
_QUEUED_WIRE = ["QUEUED", "CANCEL_QUEUED", "EDIT_QUEUED"]


def _order(
    order_id: str,
    *,
    status: str = "OPEN",
    product: str = _PAIR,
    client_order_id: str | None = None,
) -> dict[str, Any]:
    return {
        "order_id": order_id,
        "client_order_id": client_order_id or f"cid-{order_id}",
        "product_id": product,
        "side": "BUY",
        "status": status,
        "order_configuration": {"limit_limit_gtc": {"base_size": "1.0", "limit_price": "50000"}},
        "filled_size": "0",
        "average_filled_price": "0",
    }


class _FakeRest:
    """Serves ``list_orders`` in pages of 2; cursor is the start index into the filtered rows."""

    def __init__(self, orders: list[dict[str, Any]]) -> None:
        self.orders = orders
        self.calls: list[dict[str, Any]] = []

    async def request(self, endpoint_name: str, **kwargs: Any) -> Response:
        params = dict(kwargs.get("params") or {})
        if endpoint_name != "list_orders":
            raise AssertionError(f"unexpected endpoint {endpoint_name}")
        self.calls.append(params)
        status = params.get("order_status")
        statuses = [status] if isinstance(status, str) else status
        rows = [
            o
            for o in self.orders
            if params.get("product_id") in (None, o["product_id"])
            and (statuses is None or o["status"] in statuses)
        ]
        start = int(params.get("cursor") or "0")
        end = start + _PAGE
        body = {
            "orders": rows[start:end],
            "has_next": end < len(rows),
            "cursor": str(end) if end < len(rows) else "",
        }
        return Response(raw=body, status_code=200, headers=httpx.Headers(), _endpoint=endpoint_name)


def _gateway(orders: list[dict[str, Any]]) -> CoinbaseGateway:
    gw = CoinbaseGateway(CoinbaseConfig(api_key="k", secret_key="raw_hmac_secret", sandbox=True))
    gw._rest = _FakeRest(orders)  # type: ignore[assignment]
    gw._started = True
    return gw


def _calls(gw: CoinbaseGateway) -> list[dict[str, Any]]:
    return gw._rest.calls  # type: ignore[attr-defined,no-any-return]


class TestGetOpenOrdersPagination:
    async def test_returns_pair_orders_from_every_page(self) -> None:
        orders = [
            _order("B1"),
            _order("E1", product="ETH-USD"),
            _order("B2"),
            _order("E2", product="ETH-USD"),
            _order("B3"),
            _order("B4"),
            _order("E3", product="ETH-USD"),
            _order("B5"),
        ]
        gw = _gateway(orders)

        result = await gw.get_open_orders(_PAIR)

        assert sorted(o.exchange_order_id for o in result) == ["B1", "B2", "B3", "B4", "B5"]
        assert {o.trading_pair for o in result} == {_PAIR}

    async def test_stops_when_has_next_is_false(self) -> None:
        orders = [_order(f"B{i}") for i in range(5)]  # 5 rows / 2 per page = 3 pages
        gw = _gateway(orders)

        await gw.get_open_orders(_PAIR)

        assert len(_calls(gw)) == 3

    async def test_single_page_still_works(self) -> None:
        gw = _gateway([_order("B1"), _order("E1", product="ETH-USD"), _order("B2")])

        result = await gw.get_open_orders(_PAIR)

        assert sorted(o.exchange_order_id for o in result) == ["B1", "B2"]
        assert len(_calls(gw)) == 1

    async def test_empty_result_is_empty_list(self) -> None:
        gw = _gateway([_order("E1", product="ETH-USD")])

        assert await gw.get_open_orders(_PAIR) == []
        assert len(_calls(gw)) == 1

    async def test_every_page_request_filters_open_status(self) -> None:
        gw = _gateway([_order(f"B{i}") for i in range(5)] + [_order("F1", status="FILLED")])

        result = await gw.get_open_orders(_PAIR)

        assert len(result) == 5
        assert all(c["order_status"] == "OPEN" for c in _calls(gw))
        assert all(c["product_id"] == _PAIR for c in _calls(gw))


class TestQueuedStatusesParse:
    @pytest.mark.parametrize("wire", _QUEUED_WIRE)
    def test_enum_recognises_wire_value(self, wire: str) -> None:
        assert CoinbaseOrderStatus(wire).value == wire

    @pytest.mark.parametrize("wire", _QUEUED_WIRE)
    def test_order_schema_keeps_wire_status(self, wire: str) -> None:
        order = Order.model_validate(_order("X1", status=wire))

        assert order.status.value == wire

    def test_truly_unknown_status_falls_back_to_unknown(self) -> None:
        assert CoinbaseOrderStatus("SOMETHING_NEW") is CoinbaseOrderStatus.UNKNOWN
        order = Order.model_validate(_order("X1", status="SOMETHING_NEW"))
        assert order.status is CoinbaseOrderStatus.UNKNOWN


class TestQueuedStatusToOrderState:
    @pytest.mark.parametrize(
        ("wire", "expected"),
        [
            ("QUEUED", OrderState.PENDING_CREATE),
            ("CANCEL_QUEUED", OrderState.PENDING_CANCEL),
            ("EDIT_QUEUED", OrderState.OPEN),
        ],
    )
    def test_wire_payload_maps_to_order_state(self, wire: str, expected: OrderState) -> None:
        order = Order.model_validate(_order("X1", status=wire))

        assert to_order_state(order.status.value, Decimal(order.filled_size)) is expected

    def test_to_open_order_keeps_queued_status(self) -> None:
        open_order = to_open_order(Order.model_validate(_order("X1", status="QUEUED")))

        assert open_order.status == "QUEUED"


class TestQueuedLegState:
    async def test_queued_leg_reports_pending_create(self) -> None:
        gw = _gateway([_order("V-Q", status="QUEUED", client_order_id="q-1")])

        snap = await gw.get_leg(_PAIR, client_order_id="q-1")

        assert snap is not None
        assert snap.venue_order_id == "V-Q"
        assert snap.state is OrderState.PENDING_CREATE
