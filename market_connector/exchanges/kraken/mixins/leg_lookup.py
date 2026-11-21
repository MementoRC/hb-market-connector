"""LegLookupMixin: ``SupportsLegLookup`` for Kraken (#250).

Link tag: ``str(userref)`` of an order (negative int32 only), or its ``cl_ord_id`` when it
has no link userref. Kraken has no separate client id for a userref-tagged order, so
``client_order_id`` is the same string.

A bracket's released conditional close shares the entry's userref on the wire. It is
classified as leg 1 (effective tag ``encode(group, 1)``) when it has no ``descr.close`` and
either carries a ``refid`` or shares its userref with an order that has ``descr.close``.

Limitation: a lone released close with an empty ``refid`` (its entry not in the fetched set)
is indistinguishable from leg 0. Every snapshot carries ``venue_order_id`` (the txid) so
callers can reconcile by txid.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from data_type_primitives import OrderState

from market_connector.exchanges.kraken.linked_order_codec import (
    KrakenClOrdIdLinkCodec,
    KrakenUserrefLinkCodec,
)
from market_connector.exchanges.kraken.pairs import pair_names, resolve_wire_pair
from market_connector.protocols import LegSnapshot

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from market_connector.exchanges.kraken.schemas.rest import OrderInfo

_USERREF_MIN = -(2**31)
_CLOSED_STATES = {"closed": OrderState.FILLED, "canceled": OrderState.CANCELED}
_USERREF_CODEC = KrakenUserrefLinkCodec()
_CL_ORD_ID_CODEC = KrakenClOrdIdLinkCodec()


def link_tag(userref: int | None, cl_ord_id: str | None) -> str | None:
    """The leg tag of an order, else ``None`` (not link-tagged).

    Tagged means a negative userref (the link-codec range; a positive userref is the
    connector's own per-order counter) or a cl_ord_id. A userref wins over a cl_ord_id.
    """
    if userref is not None and userref < 0:
        return str(userref)
    return cl_ord_id or None


def _tag_of(info: OrderInfo) -> str | None:
    return link_tag(info.userref, info.cl_ord_id)


def _is_userref(key: str) -> bool:
    """True only for a negative int32 string (the link-codec range); else a cl_ord_id."""
    try:
        return _USERREF_MIN <= int(key) < 0
    except ValueError:
        return False


def _effective_tags(orders: Mapping[str, OrderInfo]) -> dict[str, str | None]:
    """Leg tag per txid, re-tagging released bracket closes as leg 1."""
    closers = {i.userref for i in orders.values() if i.descr.close and i.userref is not None}
    tags: dict[str, str | None] = {}
    for txid, info in orders.items():
        tag = _tag_of(info)
        decoded = _USERREF_CODEC.decode(info.userref) if info.userref is not None else None
        released = not info.descr.close and (bool(info.refid) or info.userref in closers)
        if tag is not None and decoded is not None and decoded[1] == 0 and released:
            tag = str(_USERREF_CODEC.encode(decoded[0], 1))
        tags[txid] = tag
    return tags


def _group_of(info: OrderInfo, tag: str) -> str | None:
    if info.userref is not None and info.userref < 0:
        decoded = _USERREF_CODEC.decode(info.userref)
    else:
        decoded = _CL_ORD_ID_CODEC.decode(tag)
    return None if decoded is None else str(decoded[0])


def _on_pair(info: OrderInfo, names: frozenset[str]) -> bool:
    return info.descr.pair.replace("/", "") in names


def _snapshot(txid: str, info: OrderInfo, tag: str, *, open_order: bool) -> LegSnapshot:
    filled = Decimal(info.vol_exec)
    if open_order:
        state = OrderState.PARTIALLY_FILLED if filled > 0 else OrderState.OPEN
    else:
        state = _CLOSED_STATES.get(info.status, OrderState.CANCELED)
    return LegSnapshot(
        client_order_id=tag,
        link_tag=tag,
        state=state,
        filled=filled,
        venue_order_id=txid,
        group_id=_group_of(info, tag),
    )


def _first_leg(
    orders: Mapping[str, OrderInfo], names: frozenset[str], key: str, is_open: bool
) -> LegSnapshot | None:
    tags = _effective_tags(orders)
    for txid, info in orders.items():
        tag = tags[txid]
        if tag == key and _on_pair(info, names):
            return _snapshot(txid, info, tag, open_order=is_open)
    return None


class LegLookupMixin:
    async def _find_leg(
        self: Any, names: frozenset[str], key: str, userref: int | None, cl_ord_id: str | None
    ) -> LegSnapshot | None:
        opened = await self.get_open_orders(userref, cl_ord_id=cl_ord_id)
        found = _first_leg(opened, names, key, True)
        if found is not None:
            return found
        closed = await self.get_closed_orders(userref, cl_ord_id=cl_ord_id)
        return _first_leg(closed, names, key, False)

    async def get_leg(
        self: Any,
        target: str,
        *,
        client_order_id: str | None = None,
        link_tag: str | None = None,
    ) -> LegSnapshot | None:
        """Look up one leg by ``client_order_id`` or ``link_tag`` (open orders, then closed).

        A negative int32 key is a userref, anything else a ``cl_ord_id``; the filter is
        applied by Kraken (not by page), so this costs one OpenOrders plus one ClosedOrders
        request. A leg-1 userref key that matches nothing is retried under the group's
        leg-0 userref, where a released bracket close lives. Returns ``None`` when nothing
        matches, or when no key is given.
        """
        key = client_order_id or link_tag
        if key is None:
            return None
        wire_pair = await resolve_wire_pair(self, target)
        names = pair_names(self, wire_pair)
        found: LegSnapshot | None
        if not _is_userref(key):
            found = await self._find_leg(names, key, None, key)
            return found
        found = await self._find_leg(names, key, int(key), None)
        decoded = _USERREF_CODEC.decode(int(key))
        if found is None and decoded is not None and decoded[1] == 1:
            found = await self._find_leg(names, key, _USERREF_CODEC.encode(decoded[0], 0), None)
        return found

    async def list_open_legs(
        self: Any, target: str, *, group: str | None = None
    ) -> Sequence[LegSnapshot]:
        """Open link-tagged orders on ``target`` (one OpenOrders call).

        ``group`` keeps only legs whose ``group_id`` equals it.
        """
        wire_pair = await resolve_wire_pair(self, target)
        names = pair_names(self, wire_pair)
        orders = await self.get_open_orders()
        tags = _effective_tags(orders)
        legs: list[LegSnapshot] = []
        for txid, info in orders.items():
            tag = tags[txid]
            if tag is None or not _on_pair(info, names):
                continue
            leg = _snapshot(txid, info, tag, open_order=True)
            if group is None or leg.group_id == group:
                legs.append(leg)
        return legs
