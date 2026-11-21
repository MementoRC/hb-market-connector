"""LegLookupMixin: ``SupportsLegLookup`` for Coinbase (#250).

Link tag: the codec-encoded ``client_order_id`` (``mcl-<group>-<leg>``). A foreign id
(e.g. ``coinbase-<uuid>``) is still found by ``get_leg`` but has ``link_tag``/``group_id``
``None`` and is excluded from ``list_open_legs``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from market_connector.exceptions import GatewayNotStartedError
from market_connector.exchanges.coinbase.converters import to_exchange_pair
from market_connector.exchanges.coinbase.linked_order_codec import (
    CoinbaseClientOrderIdLinkCodec,
)
from market_connector.exchanges.coinbase.order_state import to_order_state
from market_connector.exchanges.coinbase.schemas.rest import ListOrdersResponse, Order
from market_connector.protocols import LegSnapshot

if TYPE_CHECKING:
    from collections.abc import Sequence

_CODEC = CoinbaseClientOrderIdLinkCodec()


def _snapshot(order: Order) -> LegSnapshot:
    """Build a snapshot straight from the schema order.

    Deliberately avoids ``to_open_order``: it raises on stop-limit configurations and
    unknown sides, which must not abort a lookup that only needs id/status/fill.
    """
    decoded = _CODEC.decode(order.client_order_id)
    filled = Decimal(order.filled_size)
    return LegSnapshot(
        client_order_id=order.client_order_id,
        link_tag=None if decoded is None else order.client_order_id,
        state=to_order_state(order.status.value, filled),
        filled=filled,
        venue_order_id=order.order_id,
        group_id=None if decoded is None else str(decoded[0]),
        trigger_level=None,
        limit_level=None,
    )


class LegLookupMixin:
    async def _scan_orders(
        self: Any, target: str, *, open_only: bool = False, client_order_id: str | None = None
    ) -> list[Order]:
        """Orders of ``target`` across every cursor page (``get_open_orders`` reads one).

        Readiness is re-checked before each page so a concurrent ``stop()`` aborts the scan
        with ``GatewayNotStartedError`` instead of hitting a closed REST client. With
        ``client_order_id`` the scan stops at the first match on ``target``.
        """
        params: dict[str, str] = {"product_id": to_exchange_pair(target)}
        if open_only:
            params["order_status"] = "OPEN"
        orders: list[Order] = []
        while True:
            if not self.ready:
                raise GatewayNotStartedError("Gateway not started")
            response = await self._rest.request("list_orders", params=dict(params))
            parsed = ListOrdersResponse.model_validate(response.raw)
            for o in parsed.orders:
                if o.product_id != params["product_id"]:
                    continue
                if client_order_id is not None:
                    if o.client_order_id == client_order_id:
                        return [o]
                else:
                    orders.append(o)
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
        scan of ``target``'s order listing (no status filter: open and finished orders).
        Only an order on ``target`` matches. Returns ``None`` when nothing matches or no
        key is given.
        """
        key = client_order_id or link_tag
        if key is None:
            return None
        found = await self._scan_orders(target, client_order_id=key)
        return _snapshot(found[0]) if found else None

    async def list_open_legs(
        self: Any, target: str, *, group: str | None = None
    ) -> Sequence[LegSnapshot]:
        """Open link-tagged orders on ``target``; ``group`` keeps only that ``group_id``."""
        legs = [_snapshot(o) for o in await self._scan_orders(target, open_only=True)]
        return [
            leg
            for leg in legs
            if leg.group_id is not None and (group is None or leg.group_id == group)
        ]
