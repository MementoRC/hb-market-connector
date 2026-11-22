"""Cursor-paginated ``list_orders`` pages, shared by ``get_open_orders`` and leg lookup."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from market_connector.exceptions import GatewayError, GatewayNotStartedError
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
    ``GatewayNotStartedError`` instead of hitting a closed REST client. Stops when
    ``has_next`` is false. Raises ``GatewayError`` if ``has_next`` is set without a cursor or
    a cursor repeats, rather than silently returning partial results or looping.
    """
    query = dict(params)
    seen: set[str] = set()
    while True:
        if not gateway.ready:
            raise GatewayNotStartedError("Gateway not started")
        response = await gateway._rest.request("list_orders", params=dict(query))
        parsed = ListOrdersResponse.model_validate(response.raw)
        yield parsed.orders
        if not parsed.has_next:
            return
        if not parsed.cursor:
            raise GatewayError("list_orders reported has_next without a cursor")
        if parsed.cursor in seen or parsed.cursor == query.get("cursor"):
            raise GatewayError(f"list_orders repeated cursor {parsed.cursor!r}")
        seen.add(parsed.cursor)
        query["cursor"] = parsed.cursor
