"""Regression tests for PR #307 review findings (Coinbase open orders pagination)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from market_connector.exceptions import GatewayError, GatewayNotStartedError
from market_connector.exchanges.coinbase.coinbase_gateway import CoinbaseGateway
from market_connector.exchanges.coinbase.config import CoinbaseConfig
from market_connector.exchanges.coinbase.linked_order_codec import (
    CoinbaseClientOrderIdLinkCodec,
)
from market_connector.transport.response import Response

_CID = CoinbaseClientOrderIdLinkCodec().encode(5, 0)
_CID2 = CoinbaseClientOrderIdLinkCodec().encode(5, 1)


def _order(order_id: str, cid: str, status: str = "OPEN") -> dict[str, Any]:
    return {
        "order_id": order_id,
        "client_order_id": cid,
        "product_id": "BTC-USD",
        "side": "BUY",
        "status": status,
        "order_configuration": {"limit_limit_gtc": {"base_size": "1.0", "limit_price": "5"}},
        "filled_size": "0",
    }


class _Rest:
    """Serves queued bodies in order; records params; empty afterwards."""

    def __init__(self, bodies: list[dict[str, Any]]) -> None:
        self.bodies = bodies
        self.calls: list[dict[str, Any]] = []

    async def request(self, endpoint_name: str, **kwargs: Any) -> Response:
        self.calls.append(dict(kwargs.get("params") or {}))
        body = self.bodies[len(self.calls) - 1] if len(self.calls) <= len(self.bodies) else {}
        return Response(
            raw=body or {"orders": []},
            status_code=200,
            headers=httpx.Headers(),
            _endpoint=endpoint_name,
        )


def _gw(bodies: list[dict[str, Any]]) -> CoinbaseGateway:
    gw = CoinbaseGateway(CoinbaseConfig(api_key="k", secret_key="s", sandbox=True))
    gw._rest = _Rest(bodies)  # type: ignore[assignment]
    gw._started = True
    return gw


async def test_has_next_without_cursor_raises() -> None:
    gw = _gw([{"orders": [_order("V1", "a")], "has_next": True, "cursor": ""}])
    with pytest.raises(GatewayError, match="without a cursor"):
        await gw.get_open_orders("BTC-USD")


async def test_cursor_seen_on_earlier_page_raises() -> None:
    gw = _gw(
        [
            {"orders": [], "has_next": True, "cursor": "A"},
            {"orders": [], "has_next": True, "cursor": "B"},
            {"orders": [], "has_next": True, "cursor": "A"},
        ]
    )
    with pytest.raises(GatewayError, match="repeated cursor"):
        await gw.get_open_orders("BTC-USD")
    assert len(gw._rest.calls) == 3  # type: ignore[attr-defined]


async def test_stop_clears_ready_before_closing_clients() -> None:
    gw = _gw([])
    seen: list[bool] = []

    async def _noop_disconnect() -> None:
        seen.append(gw.ready)

    async def _noop_close() -> None:
        seen.append(gw.ready)

    gw._ws.disconnect = _noop_disconnect  # type: ignore[method-assign]
    gw._rest.close = _noop_close  # type: ignore[attr-defined]
    await gw.stop()
    assert seen == [False, False]
    with pytest.raises(GatewayNotStartedError):
        await gw.get_open_orders("BTC-USD")


async def test_list_open_legs_includes_edit_queued() -> None:
    gw = _gw(
        [
            {"orders": [_order("V1", _CID)]},
            {"orders": [_order("V2", _CID2, status="EDIT_QUEUED")]},
        ]
    )
    legs = await gw.list_open_legs("BTC-USD")
    assert sorted(leg.venue_order_id for leg in legs) == ["V1", "V2"]
    statuses = [c["order_status"] for c in gw._rest.calls]  # type: ignore[attr-defined]
    assert statuses == ["OPEN", "EDIT_QUEUED"]
