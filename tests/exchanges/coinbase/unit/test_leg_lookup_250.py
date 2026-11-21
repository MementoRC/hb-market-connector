"""RED acceptance tests for #250 (Coinbase): ``get_leg`` / ``list_open_legs`` (mocked REST).

Contract pinned here (none of it exists yet):

* ``CoinbaseGateway`` satisfies ``SupportsLegLookup``.
* Coinbase link tag IS the codec-encoded ``client_order_id`` (``mcl-<group>-<leg>``). A snapshot
  has ``link_tag == client_order_id`` and ``group_id == str(group)`` when the id decodes, else
  both ``None`` (a foreign id such as ``coinbase-<uuid>`` is still found by ``client_order_id``).
* ``get_leg`` matches ``client_order_id`` or ``link_tag`` across the cursor-paginated order
  listing (open AND finished orders). Unknown, or neither key given -> ``None`` (never raises),
  as on Kraken.
* ``list_open_legs`` returns only OPEN orders of the product whose id decodes via the codec;
  ``group`` is a ``str`` compared with ``str(decoded_group)``; ``None`` -> every linked leg.
* States: OPEN -> OPEN (PARTIALLY_FILLED once ``filled_size > 0``), FILLED -> FILLED,
  CANCELLED -> CANCELED; ``filled`` is ``Decimal(filled_size)``.

The fake REST honours ``product_id`` / ``order_status`` filters when sent and serves pages of 2
via ``cursor`` / ``has_next``, so the implementation may filter venue-side or client-side.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from data_type_primitives import OrderState

from market_connector.exchanges.coinbase.coinbase_gateway import CoinbaseGateway
from market_connector.exchanges.coinbase.config import CoinbaseConfig
from market_connector.exchanges.coinbase.linked_order_codec import (
    CoinbaseClientOrderIdLinkCodec,
)
from market_connector.protocols import LegSnapshot, SupportsLegLookup
from market_connector.transport.response import Response

_PAIR = "BTC-USD"
_CODEC = CoinbaseClientOrderIdLinkCodec()
_E5 = _CODEC.encode(5, 0)
_X5 = _CODEC.encode(5, 1)
_E6 = _CODEC.encode(6, 0)
_DONE = _CODEC.encode(7, 0)
_CXL = _CODEC.encode(8, 0)
_ETH = _CODEC.encode(5, 2)
_FOREIGN = "coinbase-0a1b2c3d"
_D = Decimal
_PAGE = 2


def _order(
    order_id: str,
    cid: str,
    *,
    status: str = "OPEN",
    filled: str = "0",
    product: str = _PAIR,
) -> dict[str, Any]:
    return {
        "order_id": order_id,
        "client_order_id": cid,
        "product_id": product,
        "side": "BUY",
        "status": status,
        "order_configuration": {"limit_limit_gtc": {"base_size": "1.0", "limit_price": "50000"}},
        "filled_size": filled,
        "average_filled_price": "0",
    }


_ORDERS: list[dict[str, Any]] = [
    _order("V-DONE", _DONE, status="FILLED", filled="1.0"),
    _order("V-CXL", _CXL, status="CANCELLED", filled="0.1"),
    _order("V-FOREIGN", _FOREIGN),
    _order("V-ENTRY", _E5, filled="0.4"),
    _order("V-EXIT", _X5),
    _order("V-ETH", _ETH, product="ETH-USD"),
    _order("V-E6", _E6),
]


class _FakeRest:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def request(self, endpoint_name: str, **kwargs: Any) -> Response:
        params = dict(kwargs.get("params") or {})
        self.calls.append((endpoint_name, params))
        if endpoint_name != "list_orders":
            raise AssertionError(f"unexpected endpoint {endpoint_name}")
        wanted = params.get("order_status")
        statuses = [wanted] if isinstance(wanted, str) else wanted
        rows = [
            o
            for o in _ORDERS
            if params.get("product_id") in (None, o["product_id"])
            and (statuses is None or o["status"] in statuses)
        ]
        start = int(params.get("cursor", "0"))
        end = start + _PAGE
        body = {
            "orders": rows[start:end],
            "has_next": end < len(rows),
            "cursor": str(end) if end < len(rows) else "",
        }
        return Response(raw=body, status_code=200, headers=httpx.Headers(), _endpoint=endpoint_name)


@pytest.fixture
def gateway() -> CoinbaseGateway:
    gw = CoinbaseGateway(CoinbaseConfig(api_key="k", secret_key="raw_hmac_secret", sandbox=True))
    gw._rest = _FakeRest()  # type: ignore[assignment]
    gw._started = True
    return gw


def test_gateway_is_supports_leg_lookup(gateway: CoinbaseGateway) -> None:
    assert isinstance(gateway, SupportsLegLookup)


class TestGetLegByClientOrderId:
    async def test_finds_partially_filled_open_leg(self, gateway: CoinbaseGateway) -> None:
        snap = await gateway.get_leg(_PAIR, client_order_id=_E5)

        assert snap == LegSnapshot(
            client_order_id=_E5,
            link_tag=_E5,
            state=OrderState.PARTIALLY_FILLED,
            filled=_D("0.4"),
            venue_order_id="V-ENTRY",
            group_id="5",
            trigger_level=None,
            limit_level=None,
        )

    async def test_unfilled_open_leg_is_open_with_zero_filled(
        self, gateway: CoinbaseGateway
    ) -> None:
        snap = await gateway.get_leg(_PAIR, client_order_id=_X5)

        assert snap is not None
        assert snap.state is OrderState.OPEN
        assert snap.filled == _D(0)
        assert snap.venue_order_id == "V-EXIT"

    async def test_foreign_id_found_without_link_tag_or_group(
        self, gateway: CoinbaseGateway
    ) -> None:
        snap = await gateway.get_leg(_PAIR, client_order_id=_FOREIGN)

        assert snap is not None
        assert snap.client_order_id == _FOREIGN
        assert snap.link_tag is None
        assert snap.group_id is None
        assert snap.venue_order_id == "V-FOREIGN"

    async def test_unknown_client_order_id_returns_none(self, gateway: CoinbaseGateway) -> None:
        assert await gateway.get_leg(_PAIR, client_order_id="nope") is None
        assert await gateway.get_leg(_PAIR, client_order_id=_CODEC.encode(99, 0)) is None

    async def test_found_on_a_later_cursor_page(self, gateway: CoinbaseGateway) -> None:
        # V-E6 is the last of 7 orders: page 4 of 2-per-page (or page 1 of a filtered scan).
        snap = await gateway.get_leg(_PAIR, client_order_id=_E6)

        assert snap is not None
        assert snap.venue_order_id == "V-E6"
        assert snap.group_id == "6"


class TestGetLegByLinkTag:
    async def test_link_tag_is_the_encoded_client_order_id(self, gateway: CoinbaseGateway) -> None:
        snap = await gateway.get_leg(_PAIR, link_tag=_E5)

        assert snap is not None
        assert snap.client_order_id == _E5
        assert snap.link_tag == _E5
        assert snap.state is OrderState.PARTIALLY_FILLED
        assert snap.filled == _D("0.4")

    async def test_unknown_link_tag_returns_none(self, gateway: CoinbaseGateway) -> None:
        assert await gateway.get_leg(_PAIR, link_tag=_CODEC.encode(99, 0)) is None
        assert await gateway.get_leg(_PAIR, link_tag="no-such-tag") is None

    async def test_link_tag_found_on_a_later_cursor_page(self, gateway: CoinbaseGateway) -> None:
        snap = await gateway.get_leg(_PAIR, link_tag=_E6)

        assert snap is not None
        assert snap.venue_order_id == "V-E6"


class TestGetLegFinishedOrders:
    async def test_filled_leg_reports_filled(self, gateway: CoinbaseGateway) -> None:
        snap = await gateway.get_leg(_PAIR, link_tag=_DONE)

        assert snap is not None
        assert snap.state is OrderState.FILLED
        assert snap.filled == _D("1.0")
        assert snap.group_id == "7"

    async def test_cancelled_leg_reports_canceled_with_cumulative_fill(
        self, gateway: CoinbaseGateway
    ) -> None:
        snap = await gateway.get_leg(_PAIR, client_order_id=_CXL)

        assert snap is not None
        assert snap.state is OrderState.CANCELED
        assert snap.filled == _D("0.1")


class _StrictStatusRest(_FakeRest):
    """Like venues where an omitted ``order_status`` is NOT 'all statuses': no filter -> nothing."""

    async def request(self, endpoint_name: str, **kwargs: Any) -> Response:
        if not (kwargs.get("params") or {}).get("order_status"):
            return Response(
                raw={"orders": [], "has_next": False, "cursor": ""},
                status_code=200,
                headers=httpx.Headers(),
                _endpoint=endpoint_name,
            )
        return await super().request(endpoint_name, **kwargs)


class TestGetLegExplicitStatuses:
    """The venue documents no default for ``order_status``: ``get_leg`` must ask explicitly.

    OPEN is queried alone (not known to combine with other statuses), then the non-OPEN set.
    """

    async def test_sends_open_then_terminal_status_queries(self, gateway: CoinbaseGateway) -> None:
        assert await gateway.get_leg(_PAIR, client_order_id="nope") is None

        statuses = [c[1]["order_status"] for c in gateway._rest.calls]  # type: ignore[attr-defined]
        assert "OPEN" in statuses
        terminal = [s for s in statuses if s != "OPEN"]
        assert terminal and all(s == terminal[0] for s in terminal)
        assert terminal[0] == ["FILLED", "CANCELLED", "EXPIRED", "FAILED", "QUEUED"]
        assert statuses.index("OPEN") < statuses.index(terminal[0])
        assert all(c[1]["product_id"] == _PAIR for c in gateway._rest.calls)  # type: ignore[attr-defined]

    async def test_open_leg_found_without_terminal_query(self, gateway: CoinbaseGateway) -> None:
        assert await gateway.get_leg(_PAIR, client_order_id=_E5) is not None

        statuses = [c[1]["order_status"] for c in gateway._rest.calls]  # type: ignore[attr-defined]
        assert statuses and all(s == "OPEN" for s in statuses)

    async def test_filled_and_cancelled_found_when_omitted_status_returns_nothing(self) -> None:
        gw = CoinbaseGateway(CoinbaseConfig(api_key="k", secret_key="raw_hmac_secret", sandbox=True))
        gw._rest = _StrictStatusRest()  # type: ignore[assignment]
        gw._started = True

        filled = await gw.get_leg(_PAIR, client_order_id=_DONE)
        cancelled = await gw.get_leg(_PAIR, client_order_id=_CXL)

        assert filled is not None
        assert filled.state is OrderState.FILLED
        assert cancelled is not None
        assert cancelled.state is OrderState.CANCELED
        assert cancelled.filled == _D("0.1")

    async def test_list_open_legs_still_sends_open_only(self, gateway: CoinbaseGateway) -> None:
        await gateway.list_open_legs(_PAIR)

        statuses = {c[1]["order_status"] for c in gateway._rest.calls}  # type: ignore[attr-defined]
        assert statuses == {"OPEN"}


class TestGetLegArguments:
    async def test_neither_key_is_none_not_an_error(self, gateway: CoinbaseGateway) -> None:
        assert await gateway.get_leg(_PAIR) is None

    async def test_filled_is_decimal_not_float(self, gateway: CoinbaseGateway) -> None:
        snap = await gateway.get_leg(_PAIR, client_order_id=_E5)

        assert snap is not None
        assert isinstance(snap.filled, Decimal)


class TestListOpenLegs:
    async def test_returns_only_open_linked_legs_of_the_product(
        self, gateway: CoinbaseGateway
    ) -> None:
        legs = await gateway.list_open_legs(_PAIR)

        assert all(isinstance(leg, LegSnapshot) for leg in legs)
        assert {leg.client_order_id for leg in legs} == {_E5, _X5, _E6}

    async def test_excludes_non_linked_closed_and_other_product_orders(
        self, gateway: CoinbaseGateway
    ) -> None:
        ids = {leg.client_order_id for leg in await gateway.list_open_legs(_PAIR)}

        assert _FOREIGN not in ids
        assert _DONE not in ids
        assert _CXL not in ids
        assert _ETH not in ids

    async def test_snapshots_carry_state_filled_venue_id_and_group(
        self, gateway: CoinbaseGateway
    ) -> None:
        by_cid = {leg.client_order_id: leg for leg in await gateway.list_open_legs(_PAIR)}

        entry = by_cid[_E5]
        assert (entry.state, entry.filled) == (OrderState.PARTIALLY_FILLED, _D("0.4"))
        assert (entry.link_tag, entry.venue_order_id, entry.group_id) == (_E5, "V-ENTRY", "5")
        exit_leg = by_cid[_X5]
        assert (exit_leg.state, exit_leg.filled) == (OrderState.OPEN, _D(0))

    async def test_group_filter_selects_only_that_group(self, gateway: CoinbaseGateway) -> None:
        g5 = await gateway.list_open_legs(_PAIR, group="5")
        g6 = await gateway.list_open_legs(_PAIR, group="6")

        assert {leg.client_order_id for leg in g5} == {_E5, _X5}
        assert {leg.client_order_id for leg in g6} == {_E6}

    async def test_group_none_returns_all_linked_legs(self, gateway: CoinbaseGateway) -> None:
        legs = await gateway.list_open_legs(_PAIR, group=None)

        assert len(legs) == 3

    async def test_unknown_group_is_empty(self, gateway: CoinbaseGateway) -> None:
        assert list(await gateway.list_open_legs(_PAIR, group="99")) == []
