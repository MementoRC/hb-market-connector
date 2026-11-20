"""RED acceptance tests for #250: Kraken ``get_leg`` / ``list_open_legs`` (mocked REST).

Contract pinned here (none of it exists yet):

* ``KrakenGateway`` satisfies ``SupportsLegLookup``.
* Link tag on Kraken: an order's ``userref`` (as ``str(userref)``) or, when it has no
  userref, its ``cl_ord_id``. ``client_order_id`` of a snapshot is the same string, since
  Kraken has no separate client id for a userref-tagged order.
* ``get_leg`` finds a leg by ``client_order_id`` or ``link_tag`` among open orders (and
  falls back to closed orders so a finished leg reports FILLED/CANCELED). Unknown -> ``None``,
  never an exception.
* ``list_open_legs`` returns EVERY open order carrying a tag, without decode-filtering
  (a foreign ``cl_ord_id`` leg is returned). Untagged and closed orders are excluded.
* ``filled`` is cumulative base from ``vol_exec``; ``vol_exec == 0`` -> OPEN, ``0 < vol_exec``
  while still open -> PARTIALLY_FILLED.

The fake REST honours the ``userref`` / ``cl_ord_id`` filters of OpenOrders/ClosedOrders and
serves QueryOrders by txid, so the implementation may filter venue-side or client-side.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from data_type_primitives import OrderState

from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway
from market_connector.exchanges.kraken.linked_order_codec import KrakenUserrefLinkCodec
from market_connector.protocols import LegSnapshot, SupportsLegLookup
from market_connector.transport.response import Response

_PAIR = "XBTUSD"
_CODEC = KrakenUserrefLinkCodec()
_REF_ENTRY = _CODEC.encode(5, 0)
_REF_EXIT = _CODEC.encode(5, 1)
_REF_DONE = _CODEC.encode(6, 0)
_ODD_CL = "zz~odd tag"
_D = Decimal


def _order(
    *,
    userref: int | None = None,
    cl_ord_id: str | None = None,
    status: str = "open",
    vol: str = "1.0",
    vol_exec: str = "0",
) -> dict[str, Any]:
    order: dict[str, Any] = {
        "status": status,
        "opentm": 1.0,
        "vol": vol,
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
    "O-ENTRY": _order(userref=_REF_ENTRY, vol_exec="0.4"),
    "O-EXIT": _order(userref=_REF_EXIT),
    "O-ODD": _order(cl_ord_id=_ODD_CL),
    "O-PLAIN": _order(),
}
_CLOSED: dict[str, dict[str, Any]] = {
    "O-DONE": _order(userref=_REF_DONE, status="closed", vol_exec="1.0"),
    "O-CXL": _order(cl_ord_id="cancelled-leg", status="canceled", vol_exec="0.1"),
}


def _matches(order: dict[str, Any], data: dict[str, Any]) -> bool:
    if "userref" in data and str(order.get("userref")) != str(data["userref"]):
        return False
    return not ("cl_ord_id" in data and order.get("cl_ord_id") != data["cl_ord_id"])


class _FakeRest:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.exc: Exception | None = None

    async def request(
        self, endpoint_name: str, data: dict[str, Any] | None = None, **_: Any
    ) -> Response:
        body = dict(data or {})
        self.calls.append((endpoint_name, body))
        if self.exc is not None:
            raise self.exc
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
def rest() -> _FakeRest:
    return _FakeRest()


@pytest.fixture
def gateway(rest: _FakeRest) -> KrakenGateway:
    gw = KrakenGateway(api_key="k", secret_key="c2VjcmV0")
    gw._rest = rest  # type: ignore[assignment]
    gw._started = True
    return gw


def test_gateway_is_supports_leg_lookup(gateway: KrakenGateway) -> None:
    assert isinstance(gateway, SupportsLegLookup)


class TestGetLegByLinkTag:
    async def test_finds_userref_leg_and_reports_partial_fill(self, gateway: KrakenGateway) -> None:
        snap = await gateway.get_leg(_PAIR, link_tag=str(_REF_ENTRY))

        assert isinstance(snap, LegSnapshot)
        assert snap.link_tag == str(_REF_ENTRY)
        assert snap.client_order_id == str(_REF_ENTRY)
        assert snap.state is OrderState.PARTIALLY_FILLED
        assert snap.filled == _D("0.4")

    async def test_unfilled_open_leg_is_open_with_zero_filled(self, gateway: KrakenGateway) -> None:
        snap = await gateway.get_leg(_PAIR, link_tag=str(_REF_EXIT))

        assert snap is not None
        assert snap.state is OrderState.OPEN
        assert snap.filled == _D(0)

    async def test_finds_cl_ord_id_tagged_leg(self, gateway: KrakenGateway) -> None:
        snap = await gateway.get_leg(_PAIR, link_tag=_ODD_CL)

        assert snap is not None
        assert snap.link_tag == _ODD_CL
        assert snap.client_order_id == _ODD_CL

    async def test_unknown_link_tag_returns_none(self, gateway: KrakenGateway) -> None:
        assert await gateway.get_leg(_PAIR, link_tag=str(_CODEC.encode(99, 0))) is None
        assert await gateway.get_leg(_PAIR, link_tag="no-such-cl-ord") is None


class TestGetLegByClientOrderId:
    async def test_finds_userref_leg_by_client_order_id(self, gateway: KrakenGateway) -> None:
        snap = await gateway.get_leg(_PAIR, client_order_id=str(_REF_ENTRY))

        assert snap is not None
        assert snap.client_order_id == str(_REF_ENTRY)
        assert snap.link_tag == str(_REF_ENTRY)
        assert snap.filled == _D("0.4")

    async def test_finds_cl_ord_id_leg_by_client_order_id(self, gateway: KrakenGateway) -> None:
        snap = await gateway.get_leg(_PAIR, client_order_id=_ODD_CL)

        assert snap is not None
        assert snap.client_order_id == _ODD_CL
        assert snap.state is OrderState.OPEN

    async def test_unknown_client_order_id_returns_none(self, gateway: KrakenGateway) -> None:
        assert await gateway.get_leg(_PAIR, client_order_id="nope") is None


class TestGetLegFinishedOrders:
    async def test_filled_closed_leg_reports_filled(self, gateway: KrakenGateway) -> None:
        snap = await gateway.get_leg(_PAIR, link_tag=str(_REF_DONE))

        assert snap is not None
        assert snap.state is OrderState.FILLED
        assert snap.filled == _D("1.0")

    async def test_canceled_leg_reports_canceled_with_cumulative_fill(
        self, gateway: KrakenGateway
    ) -> None:
        snap = await gateway.get_leg(_PAIR, client_order_id="cancelled-leg")

        assert snap is not None
        assert snap.state is OrderState.CANCELED
        assert snap.filled == _D("0.1")


class TestGetLegNeverRaisesForNotFound:
    async def test_not_found_with_neither_key_is_none(self, gateway: KrakenGateway) -> None:
        assert await gateway.get_leg(_PAIR) is None

    async def test_filled_is_decimal_not_float(self, gateway: KrakenGateway) -> None:
        snap = await gateway.get_leg(_PAIR, link_tag=str(_REF_ENTRY))
        assert snap is not None
        assert isinstance(snap.filled, Decimal)


class TestListOpenLegs:
    async def test_returns_every_tagged_open_order_including_odd_tag(
        self, gateway: KrakenGateway
    ) -> None:
        legs = await gateway.list_open_legs(_PAIR)

        assert {leg.link_tag for leg in legs} == {str(_REF_ENTRY), str(_REF_EXIT), _ODD_CL}
        assert all(isinstance(leg, LegSnapshot) for leg in legs)

    async def test_excludes_untagged_orders(self, gateway: KrakenGateway) -> None:
        legs = await gateway.list_open_legs(_PAIR)

        assert all(leg.link_tag is not None for leg in legs)
        assert len(legs) == 3  # O-PLAIN has neither userref nor cl_ord_id

    async def test_excludes_closed_and_cancelled_orders(self, gateway: KrakenGateway) -> None:
        tags = {leg.link_tag for leg in await gateway.list_open_legs(_PAIR)}

        assert str(_REF_DONE) not in tags
        assert "cancelled-leg" not in tags

    async def test_snapshots_carry_state_and_cumulative_filled(
        self, gateway: KrakenGateway
    ) -> None:
        by_tag = {leg.link_tag: leg for leg in await gateway.list_open_legs(_PAIR)}

        entry = by_tag[str(_REF_ENTRY)]
        assert (entry.state, entry.filled) == (OrderState.PARTIALLY_FILLED, _D("0.4"))
        exit_leg = by_tag[str(_REF_EXIT)]
        assert (exit_leg.state, exit_leg.filled) == (OrderState.OPEN, _D(0))
