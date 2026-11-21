"""LegLookupMixin: ``SupportsLegLookup`` for Coinbase (#250).

Link tag: the codec-encoded ``client_order_id`` (``mcl-<group>-<leg>``). A foreign id
(e.g. ``coinbase-<uuid>``) is still found by ``get_leg`` but has ``link_tag``/``group_id``
``None`` and is excluded from ``list_open_legs``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from market_connector.exceptions import GatewayNotStartedError
from market_connector.exchanges.coinbase.converters import to_exchange_pair, to_open_order
from market_connector.exchanges.coinbase.linked_order_codec import (
    CoinbaseClientOrderIdLinkCodec,
)
from market_connector.exchanges.coinbase.order_state import to_order_state
from market_connector.exchanges.coinbase.schemas.rest import ListOrdersResponse
from market_connector.protocols import LegSnapshot

if TYPE_CHECKING:
    from collections.abc import Sequence

    from market_connector.primitives import OpenOrder

_CODEC = CoinbaseClientOrderIdLinkCodec()


def _snapshot(order: OpenOrder) -> LegSnapshot:
    decoded = _CODEC.decode(order.client_order_id)
    return LegSnapshot(
        client_order_id=order.client_order_id,
        link_tag=None if decoded is None else order.client_order_id,
        state=to_order_state(order.status, order.filled_amount),
        filled=order.filled_amount,
        venue_order_id=order.exchange_order_id,
        group_id=None if decoded is None else str(decoded[0]),
        trigger_level=None,
        limit_level=None,
    )


class LegLookupMixin:
    async def _open_orders_all_pages(self: Any, target: str) -> list[OpenOrder]:
        """OPEN orders of ``target`` across every cursor page (``get_open_orders`` reads one)."""
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")
        params: dict[str, str] = {"product_id": to_exchange_pair(target), "order_status": "OPEN"}
        orders: list[OpenOrder] = []
        while True:
            response = await self._rest.request("list_orders", params=dict(params))
            parsed = ListOrdersResponse.model_validate(response.raw)
            orders.extend(to_open_order(o) for o in parsed.orders)
            if not (parsed.has_next and parsed.cursor) or parsed.cursor == params.get("cursor"):
                return orders
            params["cursor"] = parsed.cursor

    async def get_leg(
        self: Any,
        target: str,
        *,
        client_order_id: str | None = None,
        link_tag: str | None = None,
    ) -> LegSnapshot | None:
        """Look up one leg by ``client_order_id`` or ``link_tag`` (the same string).

        Known cost: Coinbase has no venue-side client-id filter, so this is a paginated
        full scan of the order listing (open and finished orders, all products); ``target``
        is not used to narrow it. Returns ``None`` when nothing matches or no key is given.
        """
        key = client_order_id or link_tag
        if key is None:
            return None
        order = await self.get_order_by_client_order_id(key)
        return None if order is None else _snapshot(order)

    async def list_open_legs(
        self: Any, target: str, *, group: str | None = None
    ) -> Sequence[LegSnapshot]:
        """Open link-tagged orders on ``target``; ``group`` keeps only that ``group_id``."""
        legs = [_snapshot(o) for o in await self._open_orders_all_pages(target)]
        return [
            leg
            for leg in legs
            if leg.group_id is not None and (group is None or leg.group_id == group)
        ]
