"""RED acceptance tests for PR #289 review (#250): bracket close leg, numeric cl_ord_id,
group filter, pair alias, venue_order_id.

Contract:

* ``LegSnapshot`` gains optional ``venue_order_id`` / ``group_id`` (default ``None``);
  ``SupportsLegLookup.list_open_legs`` accepts ``group=``.
* Only NEGATIVE int32 strings are userrefs; a numeric ``cl_ord_id`` is filtered by ``cl_ord_id``.
* A released bracket close shares the entry's userref; it is leg 1 (tag ``encode(g, 1)``),
  recognised via ``refid`` or by sharing the userref with an order carrying ``descr.close``.
* ``group`` filters ``list_open_legs`` to one link group.
* Orders whose ``descr.pair`` is the Kraken internal name (``XXBTZUSD``) still match the target.
"""

from __future__ import annotations

import inspect
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
_D = Decimal


def _order(
    *,
    userref: int | None = None,
    cl_ord_id: str | None = None,
    status: str = "open",
    vol_exec: str = "0",
    refid: str | None = None,
    close: str | None = None,
    pair: str = _PAIR,
) -> dict[str, Any]:
    descr: dict[str, Any] = {
        "pair": pair,
        "type": "buy",
        "ordertype": "limit",
        "price": "50000",
        "price2": "0",
        "leverage": "none",
        "order": "buy 1.0 XBTUSD @ limit 50000",
    }
    if close is not None:
        descr["close"] = close
    order: dict[str, Any] = {
        "status": status,
        "opentm": 1.0,
        "vol": "1.0",
        "vol_exec": vol_exec,
        "cost": "0",
        "fee": "0",
        "price": "0",
        "descr": descr,
    }
    if userref is not None:
        order["userref"] = userref
    if cl_ord_id is not None:
        order["cl_ord_id"] = cl_ord_id
    if refid is not None:
        order["refid"] = refid
    return order


def _matches(order: dict[str, Any], data: dict[str, Any]) -> bool:
    if "userref" in data and str(order.get("userref")) != str(data["userref"]):
        return False
    return not ("cl_ord_id" in data and order.get("cl_ord_id") != data["cl_ord_id"])


class _FakeRest:
    def __init__(
        self,
        open_orders: dict[str, dict[str, Any]],
        closed: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.open = open_orders
        self.closed = closed or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def request(
        self, endpoint_name: str, data: dict[str, Any] | None = None, **_: Any
    ) -> Response:
        body = dict(data or {})
        self.calls.append((endpoint_name, body))
        if endpoint_name == "open_orders":
            result: dict[str, Any] = {
                "open": {k: v for k, v in self.open.items() if _matches(v, body)}
            }
        elif endpoint_name == "closed_orders":
            result = {
                "closed": {k: v for k, v in self.closed.items() if _matches(v, body)},
                "count": len(self.closed),
            }
        elif endpoint_name == "query_orders":
            everything = {**self.open, **self.closed}
            result = {t: everything[t] for t in body["txid"].split(",") if t in everything}
        elif endpoint_name == "asset_pairs":
            result = {"XXBTZUSD": {"altname": "XBTUSD", "wsname": "XBT/USD"}}
        else:
            raise AssertionError(f"unexpected endpoint {endpoint_name}")
        return Response(
            raw={"error": [], "result": result},
            status_code=200,
            headers=httpx.Headers(),
            _endpoint=endpoint_name,
        )


def _gateway(
    open_orders: dict[str, dict[str, Any]], closed: dict[str, dict[str, Any]] | None = None
) -> tuple[KrakenGateway, _FakeRest]:
    rest = _FakeRest(open_orders, closed)
    gw = KrakenGateway(api_key="k", secret_key="c2VjcmV0")
    gw._rest = rest  # type: ignore[assignment]
    gw._started = True
    return gw, rest


_E5 = _CODEC.encode(5, 0)
_C5 = _CODEC.encode(5, 1)
_STOP = "close position @ stop loss 90.0"


def _bracket(*, close_refid: str | None = "OENTRY") -> dict[str, dict[str, Any]]:
    return {
        "OENTRY": _order(userref=_E5, close=_STOP),
        "OCLOSE": _order(userref=_E5, refid=close_refid),
    }


# --- Protocol -------------------------------------------------------------------------------


def test_leg_snapshot_old_constructor_still_works_and_new_fields_default_none() -> None:
    snap = LegSnapshot(client_order_id="a", link_tag="a", state=OrderState.OPEN, filled=_D(0))

    assert snap.venue_order_id is None
    assert snap.group_id is None


def test_leg_snapshot_accepts_venue_order_id_and_group_id() -> None:
    snap = LegSnapshot(
        client_order_id="a",
        link_tag="a",
        state=OrderState.OPEN,
        filled=_D(0),
        venue_order_id="O1",
        group_id="5",
    )

    assert (snap.venue_order_id, snap.group_id) == ("O1", "5")


def test_supports_leg_lookup_list_open_legs_accepts_group_keyword() -> None:
    params = inspect.signature(SupportsLegLookup.list_open_legs).parameters

    assert "group" in params
    assert params["group"].default is None


# --- venue_order_id -------------------------------------------------------------------------


async def test_single_order_snapshot_carries_venue_order_id() -> None:
    gw, _ = _gateway({"OSINGLE": _order(userref=_E5)})

    (leg,) = await gw.list_open_legs(_PAIR)
    snap = await gw.get_leg(_PAIR, link_tag=str(_E5))

    assert leg.venue_order_id == "OSINGLE"
    assert snap is not None
    assert snap.venue_order_id == "OSINGLE"


# --- K1 numeric cl_ord_id -------------------------------------------------------------------


async def test_numeric_cl_ord_id_is_filtered_by_cl_ord_id_not_userref() -> None:
    gw, rest = _gateway({"ONUM": _order(cl_ord_id="123456")})

    snap = await gw.get_leg(_PAIR, client_order_id="123456")

    assert snap is not None
    assert snap.client_order_id == "123456"
    open_bodies = [b for n, b in rest.calls if n == "open_orders"]
    assert open_bodies
    assert all(b.get("cl_ord_id") == "123456" and "userref" not in b for b in open_bodies)


# --- K2 / K3 bracket entry vs released close -------------------------------------------------


@pytest.mark.parametrize("close_refid", ["OENTRY", None], ids=["with_refid", "without_refid"])
async def test_bracket_entry_and_released_close_are_distinct_legs(close_refid: str | None) -> None:
    gw, _ = _gateway(_bracket(close_refid=close_refid))

    legs = await gw.list_open_legs(_PAIR)

    by_venue = {leg.venue_order_id: leg for leg in legs}
    assert set(by_venue) == {"OENTRY", "OCLOSE"}
    assert len(legs) == 2
    entry, close = by_venue["OENTRY"], by_venue["OCLOSE"]
    assert entry.link_tag == entry.client_order_id == str(_E5)
    assert close.link_tag == close.client_order_id == str(_C5)
    assert entry.group_id == close.group_id == "5"


@pytest.mark.parametrize("close_refid", ["OENTRY", None], ids=["with_refid", "without_refid"])
async def test_get_leg_resolves_close_and_entry_tags_separately(close_refid: str | None) -> None:
    gw, _ = _gateway(_bracket(close_refid=close_refid))

    close = await gw.get_leg(_PAIR, link_tag=str(_C5))
    entry = await gw.get_leg(_PAIR, link_tag=str(_E5))

    assert close is not None
    assert close.venue_order_id == "OCLOSE"
    assert entry is not None
    assert entry.venue_order_id == "OENTRY"


# --- K4 lone released close, entry filled ---------------------------------------------------


async def test_lone_released_close_with_filled_entry() -> None:
    gw, _ = _gateway(
        {"OCLOSE": _order(userref=_E5, refid="OENTRY")},
        {"OENTRY": _order(userref=_E5, status="closed", vol_exec="1.0", close=_STOP)},
    )

    legs = await gw.list_open_legs(_PAIR)
    entry = await gw.get_leg(_PAIR, link_tag=str(_E5))

    assert len(legs) == 1
    assert legs[0].link_tag == str(_C5)
    assert legs[0].venue_order_id == "OCLOSE"
    assert entry is not None
    assert entry.state is OrderState.FILLED
    assert entry.venue_order_id == "OENTRY"


# --- K5 group filter ------------------------------------------------------------------------


def _grouped() -> dict[str, dict[str, Any]]:
    return {
        "O5A": _order(userref=_CODEC.encode(5, 0)),
        "O5B": _order(userref=_CODEC.encode(5, 1)),
        "O6A": _order(userref=_CODEC.encode(6, 0)),
        "OPLAIN": _order(userref=42),
    }


async def test_group_filter_selects_only_that_group() -> None:
    gw, _ = _gateway(_grouped())

    g5 = await gw.list_open_legs(_PAIR, group="5")
    g6 = await gw.list_open_legs(_PAIR, group="6")

    assert {leg.venue_order_id for leg in g5} == {"O5A", "O5B"}
    assert {leg.venue_order_id for leg in g6} == {"O6A"}


async def test_group_none_returns_all_tagged_legs() -> None:
    gw, _ = _gateway(_grouped())

    legs = await gw.list_open_legs(_PAIR, group=None)

    assert {leg.link_tag for leg in legs} == {
        str(_CODEC.encode(5, 0)),
        str(_CODEC.encode(5, 1)),
        str(_CODEC.encode(6, 0)),
    }


async def test_group_filter_unknown_group_is_empty() -> None:
    gw, _ = _gateway(_grouped())

    assert list(await gw.list_open_legs(_PAIR, group="99")) == []


# --- K6 pair alias --------------------------------------------------------------------------


async def test_internal_pair_name_matches_wire_pair_target() -> None:
    gw, _ = _gateway({"OALIAS": _order(userref=_E5, pair="XXBTZUSD")})

    legs = await gw.list_open_legs("BTC-USD")
    snap = await gw.get_leg("BTC-USD", link_tag=str(_E5))

    assert [leg.venue_order_id for leg in legs] == ["OALIAS"]
    assert snap is not None
    assert snap.venue_order_id == "OALIAS"


# --- K7 guard -------------------------------------------------------------------------------


async def test_unknown_id_returns_none() -> None:
    gw, _ = _gateway({"OSINGLE": _order(userref=_E5)})

    assert await gw.get_leg(_PAIR, client_order_id="no-such-leg") is None
    assert await gw.get_leg(_PAIR, link_tag=str(_CODEC.encode(99, 0))) is None
