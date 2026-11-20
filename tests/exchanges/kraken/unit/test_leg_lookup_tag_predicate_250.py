"""Acceptance tests for #250: the Kraken link-tag sign predicate (mocked REST).

Agreed with strategy-framework:

* A POSITIVE userref with no ``cl_ord_id`` is the connector's own per-order counter, so it is
  NOT a link tag: ``list_open_legs`` excludes it.
* A NEGATIVE userref is a link tag, ``link_tag == str(userref)``.
* A ``cl_ord_id`` always tags the order; with a negative userref the userref wins.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from data_type_primitives import OrderState

from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway
from market_connector.protocols import LegSnapshot
from market_connector.transport.response import Response

_PAIR = "XBTUSD"
_NEG_REF = -4242
_POS_REF = 777
_CL = "my-cl-ord"


def _order(
    *,
    userref: int | None = None,
    cl_ord_id: str | None = None,
    status: str = "open",
    vol_exec: str = "0",
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


_OPEN: dict[str, dict[str, Any]] = {
    "O-POS": _order(userref=_POS_REF),
    "O-NEG": _order(userref=_NEG_REF),
    "O-POS-CL": _order(userref=_POS_REF + 1, cl_ord_id="pos-cl"),
    "O-NEG-CL": _order(userref=_NEG_REF - 1, cl_ord_id="neg-cl"),
}
_CLOSED: dict[str, dict[str, Any]] = {
    "O-EXP": _order(cl_ord_id="expired-leg", status="expired", vol_exec="0.2"),
}


def _matches(order: dict[str, Any], data: dict[str, Any]) -> bool:
    if "userref" in data and str(order.get("userref")) != str(data["userref"]):
        return False
    return not ("cl_ord_id" in data and order.get("cl_ord_id") != data["cl_ord_id"])


class _FakeRest:
    async def request(
        self, endpoint_name: str, data: dict[str, Any] | None = None, **_: Any
    ) -> Response:
        body = dict(data or {})
        if endpoint_name == "open_orders":
            result: dict[str, Any] = {"open": {k: v for k, v in _OPEN.items() if _matches(v, body)}}
        elif endpoint_name == "closed_orders":
            result = {
                "closed": {k: v for k, v in _CLOSED.items() if _matches(v, body)},
                "count": len(_CLOSED),
            }
        elif endpoint_name == "query_orders":
            everything = {**_OPEN, **_CLOSED}
            result = {t: everything[t] for t in body["txid"].split(",") if t in everything}
        else:
            raise AssertionError(f"unexpected endpoint {endpoint_name}")
        return Response(
            raw={"error": [], "result": result},
            status_code=200,
            headers=httpx.Headers(),
            _endpoint=endpoint_name,
        )


@pytest.fixture
def gateway() -> KrakenGateway:
    gw = KrakenGateway(api_key="k", secret_key="c2VjcmV0")
    gw._rest = _FakeRest()  # type: ignore[assignment]
    gw._started = True
    return gw


class TestListOpenLegsTagPredicate:
    async def test_positive_userref_without_cl_ord_id_is_not_tagged(
        self, gateway: KrakenGateway
    ) -> None:
        legs = await gateway.list_open_legs(_PAIR)

        assert str(_POS_REF) not in {leg.link_tag for leg in legs}
        assert str(_POS_REF) not in {leg.client_order_id for leg in legs}

    async def test_negative_userref_is_tagged_by_userref(self, gateway: KrakenGateway) -> None:
        by_tag = {leg.link_tag: leg for leg in await gateway.list_open_legs(_PAIR)}

        assert str(_NEG_REF) in by_tag
        assert isinstance(by_tag[str(_NEG_REF)], LegSnapshot)

    async def test_positive_userref_with_cl_ord_id_is_tagged_by_cl_ord_id(
        self, gateway: KrakenGateway
    ) -> None:
        tags = {leg.link_tag for leg in await gateway.list_open_legs(_PAIR)}

        assert "pos-cl" in tags
        assert str(_POS_REF + 1) not in tags

    async def test_negative_userref_with_cl_ord_id_userref_wins(
        self, gateway: KrakenGateway
    ) -> None:
        tags = {leg.link_tag for leg in await gateway.list_open_legs(_PAIR)}

        assert str(_NEG_REF - 1) in tags
        assert "neg-cl" not in tags

    async def test_exact_tag_set(self, gateway: KrakenGateway) -> None:
        tags = {leg.link_tag for leg in await gateway.list_open_legs(_PAIR)}

        assert tags == {str(_NEG_REF), "pos-cl", str(_NEG_REF - 1)}


class TestGetLegTagPredicate:
    async def test_get_leg_by_negative_userref_link_tag(self, gateway: KrakenGateway) -> None:
        snap = await gateway.get_leg(_PAIR, link_tag=str(_NEG_REF))

        assert snap is not None
        assert snap.link_tag == str(_NEG_REF)
        assert snap.state is OrderState.OPEN

    async def test_get_leg_closed_expired_order_maps_to_canceled(
        self, gateway: KrakenGateway
    ) -> None:
        snap = await gateway.get_leg(_PAIR, client_order_id="expired-leg")

        assert snap is not None
        assert snap.state is OrderState.CANCELED
