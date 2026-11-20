"""LegLookupMixin: ``SupportsLegLookup`` for Kraken (#250).

Link tag: ``str(userref)`` of an order, or its ``cl_ord_id`` when it has no userref. Kraken
has no separate client id for a userref-tagged order, so ``client_order_id`` is the same
string. Tags are returned as found, never decoded.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from data_type_primitives import OrderState

from market_connector.exchanges.kraken.pairs import resolve_wire_pair
from market_connector.protocols import LegSnapshot

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from market_connector.exchanges.kraken.schemas.rest import OrderInfo

_USERREF_MIN = -(2**31)
_USERREF_MAX = 2**31 - 1
_CLOSED_STATES = {"closed": OrderState.FILLED, "canceled": OrderState.CANCELED}


def _tag_of(info: OrderInfo) -> str | None:
    """The leg tag of an order, else ``None`` (not link-tagged).

    Tagged means a negative userref (the link-codec range; a positive userref is the
    connector's own per-order counter) or a cl_ord_id. A userref wins over a cl_ord_id.
    """
    if info.userref is not None and info.userref < 0:
        return str(info.userref)
    return info.cl_ord_id or None


def _is_userref(key: str) -> bool:
    try:
        return _USERREF_MIN <= int(key) <= _USERREF_MAX
    except ValueError:
        return False


def _on_pair(info: OrderInfo, wire_pair: str) -> bool:
    return info.descr.pair.replace("/", "") == wire_pair.replace("/", "")


def _snapshot(info: OrderInfo, tag: str, *, open_order: bool) -> LegSnapshot:
    filled = Decimal(info.vol_exec)
    if open_order:
        state = OrderState.PARTIALLY_FILLED if filled > 0 else OrderState.OPEN
    else:
        state = _CLOSED_STATES.get(info.status, OrderState.CANCELED)
    return LegSnapshot(client_order_id=tag, link_tag=tag, state=state, filled=filled)


def _first_leg(
    orders: Mapping[str, OrderInfo], wire_pair: str, key: str, link_tag: str | None, is_open: bool
) -> LegSnapshot | None:
    for info in orders.values():
        tag = _tag_of(info)
        if tag == key and (link_tag is None or tag == link_tag) and _on_pair(info, wire_pair):
            return _snapshot(info, tag, open_order=is_open)
    return None


class LegLookupMixin:
    async def get_leg(
        self: Any,
        target: str,
        *,
        client_order_id: str | None = None,
        link_tag: str | None = None,
    ) -> LegSnapshot | None:
        """Look up one leg by ``client_order_id`` or ``link_tag`` (open orders, then closed).

        An int32 key is a userref, anything else a ``cl_ord_id``; the filter is applied
        by Kraken, so this costs at most one OpenOrders plus one ClosedOrders request
        (first page). Returns ``None`` when nothing matches, or when no key is given.
        """
        key = client_order_id or link_tag
        if key is None:
            return None
        wire_pair = await resolve_wire_pair(self, target)
        userref = int(key) if _is_userref(key) else None
        cl_ord_id = None if userref is not None else key
        opened = await self.get_open_orders(userref, cl_ord_id=cl_ord_id)
        found = _first_leg(opened, wire_pair, key, link_tag, True)
        if found is not None:
            return found
        closed = await self.get_closed_orders(userref, cl_ord_id=cl_ord_id)
        return _first_leg(closed, wire_pair, key, link_tag, False)

    async def list_open_legs(self: Any, target: str) -> Sequence[LegSnapshot]:
        """Every open order on ``target`` carrying a userref or cl_ord_id (one OpenOrders call)."""
        wire_pair = await resolve_wire_pair(self, target)
        legs: list[LegSnapshot] = []
        for info in (await self.get_open_orders()).values():
            tag = _tag_of(info)
            if tag is not None and _on_pair(info, wire_pair):
                legs.append(_snapshot(info, tag, open_order=True))
        return legs
