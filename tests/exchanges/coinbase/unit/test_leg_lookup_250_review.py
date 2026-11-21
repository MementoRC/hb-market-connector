"""Regression tests for PR #302 review findings (Coinbase leg lookup)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from data_type_primitives import OrderState

from market_connector.exceptions import GatewayNotStartedError
from market_connector.exchanges.coinbase.coinbase_gateway import CoinbaseGateway
from market_connector.exchanges.coinbase.config import CoinbaseConfig
from market_connector.exchanges.coinbase.linked_order_codec import (
    CoinbaseClientOrderIdLinkCodec,
)
from market_connector.transport.response import Response

_CID = CoinbaseClientOrderIdLinkCodec().encode(5, 0)


def _order(order_id: str, cid: str, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "order_id": order_id,
        "client_order_id": cid,
        "product_id": "BTC-USD",
        "side": "BUY",
        "status": "OPEN",
        "order_configuration": {"limit_limit_gtc": {"base_size": "1.0", "limit_price": "5"}},
        "filled_size": "0",
    }
    base.update(over)
    return base


class _Rest:
    def __init__(self, pages: list[dict[str, Any]], on_request: Any = None) -> None:
        self.pages = pages
        self.n = 0
        self.on_request = on_request

    async def request(self, endpoint_name: str, **kwargs: Any) -> Response:
        body = self.pages[self.n]
        self.n += 1
        if self.on_request is not None:
            self.on_request()
        return Response(raw=body, status_code=200, headers=httpx.Headers(), _endpoint=endpoint_name)


def _gw(pages: list[dict[str, Any]], on_request: Any = None) -> CoinbaseGateway:
    gw = CoinbaseGateway(CoinbaseConfig(api_key="k", secret_key="s", sandbox=True))
    gw._rest = _Rest(pages, on_request)  # type: ignore[assignment]
    gw._started = True
    return gw


async def test_order_on_other_product_does_not_match() -> None:
    gw = _gw([{"orders": [_order("V1", _CID, product_id="ETH-USD")]}])
    assert await gw.get_leg("BTC-USD", client_order_id=_CID) is None


async def test_unknown_status_and_side_do_not_abort_lookup() -> None:
    gw = _gw(
        [
            {
                "orders": [
                    _order("V0", "x", status="BRAND_NEW_STATUS", side="UNKNOWN_ORDER_SIDE"),
                    _order("V1", _CID),
                ]
            }
        ]
    )
    snap = await gw.get_leg("BTC-USD", client_order_id=_CID)
    assert snap is not None
    assert snap.venue_order_id == "V1"


async def test_unknown_status_maps_to_unknown_state() -> None:
    gw = _gw([{"orders": [_order("V1", _CID, status="BRAND_NEW_STATUS")]}])
    snap = await gw.get_leg("BTC-USD", client_order_id=_CID)
    assert snap is not None
    assert snap.state is OrderState.UNKNOWN


async def test_stop_limit_order_does_not_abort_scan() -> None:
    stop_cfg = {
        "stop_limit_stop_limit_gtc": {
            "base_size": "1",
            "limit_price": "5",
            "stop_price": "6",
            "stop_direction": "STOP_DIRECTION_STOP_DOWN",
        }
    }
    gw = _gw(
        [
            {
                "orders": [
                    _order("V0", "stop-1", order_configuration=stop_cfg),
                    _order("V1", _CID, filled_size="0.5"),
                ]
            }
        ]
    )
    legs = await gw.list_open_legs("BTC-USD")
    assert [leg.venue_order_id for leg in legs] == ["V1"]
    assert legs[0].filled == Decimal("0.5")


async def test_stop_between_pages_raises_not_started() -> None:
    holder: list[CoinbaseGateway] = []

    def stop_now() -> None:
        holder[0]._started = False

    gw = _gw(
        [
            {"orders": [_order("V0", "a")], "has_next": True, "cursor": "1"},
            {"orders": [_order("V1", _CID)]},
        ],
        on_request=stop_now,
    )
    holder.append(gw)
    with pytest.raises(GatewayNotStartedError):
        await gw.get_leg("BTC-USD", client_order_id=_CID)
