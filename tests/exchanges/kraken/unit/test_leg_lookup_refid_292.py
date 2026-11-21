"""RED tests for #292: a released conditional close is leg 1 via ``refid`` alone.

Live fact (2026-11-21): a released Kraken conditional close carries ``refid`` = the entry's
txid and inherits the entry's ``userref``. Rule pinned: empty ``descr.close`` plus non-empty
``refid`` means leg 1, whether or not the entry is still listed.

The fixture keeps the live order's shape and field values that matter (``refid`` present,
``userref`` -19, empty ``descr.close``, ``descr.ordertype`` limit) but txid, refid, price and
open time are synthetic, so no live account activity is published. Observed on the live
close: ``cl_ord_id`` was ABSENT (no such key), so cl_ord_id inheritance is unobserved. The
``userref`` -19 value was a probe value (it decodes to group 1, leg 2; our codec never
puts an entry on leg 2), which is why the tests re-encode it to ``encode(1, 0)``, the
entry's tag; assertions use group 1 and leg 1.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway
from market_connector.exchanges.kraken.linked_order_codec import KrakenUserrefLinkCodec
from market_connector.transport.response import Response

_PAIR = "XBTUSD"
_TXID = "OAAAAA-AAAAA-AAAAAA"
_ENTRY_TXID = "OBBBBB-BBBBB-BBBBBB"  # the entry the close was released from (fixture refid)
_CODEC = KrakenUserrefLinkCodec()
_FIXTURE = (
    Path(__file__).parent.parent / "fixtures" / "rest" / "open_orders_released_close_292.json"
)
_GROUP = _CODEC.decode(-19)[0]  # type: ignore[index]
_ENTRY_REF = _CODEC.encode(_GROUP, 0)
_CLOSE_TAG = str(_CODEC.encode(_GROUP, 1))


def _open_orders() -> dict[str, Any]:
    raw = json.loads(_FIXTURE.read_text())
    return copy.deepcopy(raw["result"]["open"])


def _released_close() -> dict[str, dict[str, Any]]:
    orders = _open_orders()
    orders[_TXID]["userref"] = _ENTRY_REF
    return orders


class _FakeRest:
    def __init__(self, open_orders: dict[str, dict[str, Any]]) -> None:
        self.open_orders = open_orders

    async def request(
        self, endpoint_name: str, data: dict[str, Any] | None = None, **_: Any
    ) -> Response:
        body = dict(data or {})
        if endpoint_name == "open_orders":
            result: dict[str, Any] = {
                "open": {
                    k: v
                    for k, v in self.open_orders.items()
                    if "userref" not in body or str(v.get("userref")) == str(body["userref"])
                }
            }
        elif endpoint_name == "closed_orders":
            result = {"closed": {}, "count": 0}
        else:
            raise AssertionError(f"unexpected endpoint {endpoint_name}")
        return Response(
            raw={"error": [], "result": result},
            status_code=200,
            headers=httpx.Headers(),
            _endpoint=endpoint_name,
        )


def _gateway(open_orders: dict[str, dict[str, Any]]) -> KrakenGateway:
    gw = KrakenGateway(api_key="k", secret_key="c2VjcmV0")
    gw._rest = _FakeRest(open_orders)  # type: ignore[assignment]
    gw._started = True
    return gw


def test_fixture_userref_decodes_to_group_1_leg_2() -> None:
    assert _CODEC.decode(-19) == (1, 2)
    assert _GROUP == 1


def test_fixture_records_released_close_fields() -> None:
    order = _open_orders()[_TXID]

    # Live fact (module docstring): refid is the ENTRY's txid, not the close's own. The fixture
    # entry txid is synthetic, but the relationship (refid names another order) is pinned.
    assert order["refid"] == _ENTRY_TXID
    assert order["refid"] != _TXID
    assert order["userref"] == -19
    assert order["descr"]["ordertype"] == "limit"
    assert order["descr"]["close"] == ""
    assert "cl_ord_id" not in order  # observed absent on the live close


class TestLoneReleasedClose:
    async def test_get_leg_classifies_lone_close_as_leg_1(self) -> None:
        gw = _gateway(_released_close())

        snap = await gw.get_leg(_PAIR, link_tag=_CLOSE_TAG)

        assert snap is not None
        assert snap.link_tag == _CLOSE_TAG
        assert snap.venue_order_id == _TXID

    async def test_list_open_legs_returns_fixture_as_leg_1(self) -> None:
        gw = _gateway(_released_close())

        legs = await gw.list_open_legs(_PAIR)

        assert len(legs) == 1
        assert legs[0].link_tag == _CLOSE_TAG
        assert legs[0].client_order_id == _CLOSE_TAG
        assert legs[0].group_id == str(_GROUP)
        assert legs[0].venue_order_id == _TXID

    async def test_list_open_legs_group_filter_matches_userref_group(self) -> None:
        gw = _gateway(_released_close())

        assert len(await gw.list_open_legs(_PAIR, group=str(_GROUP))) == 1
        assert await gw.list_open_legs(_PAIR, group=str(_GROUP + 1)) == []


class TestNoRefidStaysLeg0:
    @pytest.fixture
    def orders(self) -> dict[str, dict[str, Any]]:
        orders = _released_close()
        orders[_TXID]["refid"] = None
        return orders

    async def test_empty_refid_no_close_no_sibling_is_leg_0(
        self, orders: dict[str, dict[str, Any]]
    ) -> None:
        legs = await _gateway(orders).list_open_legs(_PAIR)

        assert len(legs) == 1
        assert legs[0].link_tag == str(_ENTRY_REF)
        assert legs[0].group_id == str(_GROUP)

    async def test_empty_string_refid_is_leg_0(self, orders: dict[str, dict[str, Any]]) -> None:
        orders[_TXID]["refid"] = ""

        legs = await _gateway(orders).list_open_legs(_PAIR)

        assert [leg.link_tag for leg in legs] == [str(_ENTRY_REF)]
