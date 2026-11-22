"""Cursor-paginated ``list_orders`` pages, shared by ``get_open_orders`` and leg lookup."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from market_connector.exceptions import GatewayNotStartedError
from market_connector.exchanges.coinbase.schemas.rest import ListOrdersResponse, Order

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from market_connector.exchanges.coinbase.mixins.protocols import HasReady, HasRest


async def iter_order_pages(
    gateway: HasRest & HasReady,  # type: ignore[valid-type]
    params: dict[str, Any],
) -> AsyncIterator[list[Order]]:
    """Yield each ``list_orders`` page, following ``cursor`` while ``has_next``.

    Readiness is re-checked before each page so a concurrent ``stop()`` aborts with
    ``GatewayNotStartedError`` instead of hitting a closed REST client. Stops when the
    venue returns no next cursor or repeats the previous one.
    """
    query = dict(params)
    while True:
        if not gateway.ready:
            raise GatewayNotStartedError("Gateway not started")
        response = await gateway._rest.request("list_orders", params=dict(query))
        parsed = ListOrdersResponse.model_validate(response.raw)
        yield parsed.orders
        if not (parsed.has_next and parsed.cursor) or parsed.cursor == query.get("cursor"):
            return
        query["cursor"] = parsed.cursor
