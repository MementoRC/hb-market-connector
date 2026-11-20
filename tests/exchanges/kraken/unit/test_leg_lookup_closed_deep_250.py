"""RED acceptance tests for #250: closed-leg lookup must not depend on ClosedOrders page 1.

A leg that filled or was cancelled during a long downtime sits past the first ClosedOrders
page. ``get_leg`` must still find it, deterministically, with bounded REST volume.

The fake REST models Kraken pagination: an UNFILTERED ``closed_orders`` returns only the
``_PAGE`` most recent orders, which never include the target. Per the Kraken API docs,
``open_orders`` and ``closed_orders`` both honour ``userref`` and ``cl_ord_id`` filters
server-side, so a filtered result is not limited to the first page.

Volume bound: at most one ``open_orders`` request plus one closed-side request
(``closed_orders`` or ``query_orders``) per ``get_leg``, the latter carrying the key.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from data_type_primitives import OrderState

from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway
from market_connector.exchanges.kraken.linked_order_codec import KrakenUserrefLinkCodec
from market_connector.transport.response import Response

_PAIR = "XBTUSD"
_PAGE = 5
_REF_DEEP = KrakenUserrefLinkCodec().encode(7, 0)
_CL_DEEP = "deep-cancel-leg"
_D = Decimal


def _order(
    *,
    userref: int | None = None,
    cl_ord_id: str | None = None,
    status: str = "closed",
    vol_exec: str = "1.0",
) -> dict[str, Any]:
    order: dict[str, Any] = {
        "status": status,
        "opentm": 1.0,
        "vol": "1.0",
        "vol_exec": vol_exec,
        "cost": "0",
        "fee": "0",
        "price": "0",
        "descr": {
            "pair": _PAIR,
            "type": "buy",
            "ordertype": "limit",
            "price": "50000",
            "price2": "0",
            "leverage": "none",
            "order": "buy 1.0 XBTUSD @ limit 50000",
        },
    }
    if userref is not None:
        order["userref"] = userref
    if cl_ord_id is not None:
        order["cl_ord_id"] = cl_ord_id
    return order


_RECENT: dict[str, dict[str, Any]] = {f"O-RECENT-{i}": _order(userref=i + 1) for i in range(_PAGE)}
_DEEP: dict[str, dict[str, Any]] = {
    "O-DEEP-FILLED": _order(userref=_REF_DEEP),
    "O-DEEP-CXL": _order(cl_ord_id=_CL_DEEP, status="canceled", vol_exec="0.25"),
}
_ALL_CLOSED = {**_RECENT, **_DEEP}


class _FakeRest:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    # Kraken docs: ClosedOrders `cl_ord_id` "Restricts results to the given client order ID"
    # (added 5 Sep 2024), plus `userref`; 50/page, most recent first; `ofs` is deprecated.
    # https://docs.kraken.com/api/docs/rest-api/get-closed-orders
    # https://docs.kraken.com/api/docs/change-log
    def _filtered(self, body: dict[str, Any]) -> dict[str, Any] | None:
        if "userref" in body:
            return {
                k: v
                for k, v in _ALL_CLOSED.items()
                if str(v.get("userref")) == str(body["userref"])
            }
        if "cl_ord_id" in body:
            return {k: v for k, v in _ALL_CLOSED.items() if v.get("cl_ord_id") == body["cl_ord_id"]}
        return None

    def _closed(self, body: dict[str, Any]) -> dict[str, Any]:
        hits = self._filtered(body)
        if hits is None:
            hits = dict(_RECENT)  # unfiltered: first page only
        return {"closed": hits, "count": len(_ALL_CLOSED)}

    async def request(
        self, endpoint_name: str, data: dict[str, Any] | None = None, **_: Any
    ) -> Response:
        body = dict(data or {})
        self.calls.append((endpoint_name, body))
        if endpoint_name == "open_orders":
            result: dict[str, Any] = {"open": {}}
        elif endpoint_name == "closed_orders":
            result = self._closed(body)
        else:
            raise AssertionError(f"unexpected endpoint {endpoint_name}")
        return Response(
            raw={"error": [], "result": result},
            status_code=200,
            headers=httpx.Headers(),
            _endpoint=endpoint_name,
        )

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


@pytest.fixture
def rest() -> _FakeRest:
    return _FakeRest()


@pytest.fixture
def gateway(rest: _FakeRest) -> KrakenGateway:
    gw = KrakenGateway(api_key="k", secret_key="c2VjcmV0")
    gw._rest = rest  # type: ignore[assignment]
    gw._started = True
    return gw


def _assert_bounded_and_keyed(rest: _FakeRest, key: str) -> None:
    names = rest.names()
    assert names.count("open_orders") <= 1
    closed_side = [b for n, b in rest.calls if n == "closed_orders"]
    assert len(closed_side) == 1, f"expected exactly one closed lookup, got {names}"
    body = closed_side[0]
    assert key in {str(v) for v in body.values()}, f"closed lookup carries no key filter: {body}"


class TestClosedLegPastFirstPage:
    async def test_filled_userref_leg_past_page_one_is_found(
        self, gateway: KrakenGateway, rest: _FakeRest
    ) -> None:
        snap = await gateway.get_leg(_PAIR, link_tag=str(_REF_DEEP))

        assert snap is not None
        assert snap.state is OrderState.FILLED
        assert snap.filled == _D("1.0")
        assert snap.link_tag == str(_REF_DEEP)
        _assert_bounded_and_keyed(rest, str(_REF_DEEP))

    async def test_canceled_cl_ord_id_leg_past_page_one_is_found(
        self, gateway: KrakenGateway, rest: _FakeRest
    ) -> None:
        snap = await gateway.get_leg(_PAIR, client_order_id=_CL_DEEP)

        assert snap is not None
        assert snap.state is OrderState.CANCELED
        assert snap.filled == _D("0.25")
        assert snap.client_order_id == _CL_DEEP
        _assert_bounded_and_keyed(rest, _CL_DEEP)

    async def test_request_volume_is_bounded(self, gateway: KrakenGateway, rest: _FakeRest) -> None:
        await gateway.get_leg(_PAIR, client_order_id=_CL_DEEP)

        assert len(rest.calls) <= 2


class TestClosedRequestBody:
    async def test_cid_lookup_sends_string_cl_ord_id_and_no_ofs(
        self, gateway: KrakenGateway, rest: _FakeRest
    ) -> None:
        await gateway.get_leg(_PAIR, client_order_id=_CL_DEEP)

        (body,) = [b for n, b in rest.calls if n == "closed_orders"]
        assert body["cl_ord_id"] == _CL_DEEP
        assert isinstance(body["cl_ord_id"], str)
        assert "userref" not in body
        assert "ofs" not in body

    async def test_link_tag_lookup_sends_int_userref_and_no_ofs(
        self, gateway: KrakenGateway, rest: _FakeRest
    ) -> None:
        await gateway.get_leg(_PAIR, link_tag=str(_REF_DEEP))

        (body,) = [b for n, b in rest.calls if n == "closed_orders"]
        assert _REF_DEEP < 0
        assert body["userref"] == _REF_DEEP
        assert isinstance(body["userref"], int)
        assert "cl_ord_id" not in body
        assert "ofs" not in body


class TestAbsentOrder:
    async def test_absent_userref_returns_none(self, gateway: KrakenGateway) -> None:
        missing = KrakenUserrefLinkCodec().encode(99, 0)
        assert await gateway.get_leg(_PAIR, link_tag=str(missing)) is None

    async def test_absent_cl_ord_id_returns_none(self, gateway: KrakenGateway) -> None:
        assert await gateway.get_leg(_PAIR, client_order_id="no-such-leg") is None
