"""LegLookupMixin: ``SupportsLegLookup`` for Interactive Brokers (#250).

Link tag: the ``Order.orderRef`` string (``mcl-<group>-<leg>``). ``client_order_id`` is
``str(order.orderId)``, unique only within one API client id. A foreign or empty ``orderRef``
is still found by ``get_leg`` (``link_tag``/``group_id`` ``None``) and excluded from
``list_open_legs``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from market_connector.exchanges.interactive_brokers.linked_order_codec import IbOrderRefLinkCodec
from market_connector.exchanges.interactive_brokers.order_state import to_order_state
from market_connector.protocols import LegSnapshot

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

_CODEC = IbOrderRefLinkCodec()


def _snapshot(trade: Any) -> LegSnapshot:
    order, status = trade.order, trade.orderStatus
    ref = order.orderRef
    decoded = _CODEC.decode(ref) if ref else None
    filled = Decimal(str(status.filled)) if status.filled is not None else Decimal(0)
    return LegSnapshot(
        client_order_id=str(order.orderId),
        link_tag=None if decoded is None else ref,
        state=to_order_state(status.status, filled),
        filled=filled,
        venue_order_id=str(order.permId) if order.permId else None,
        group_id=None if decoded is None else str(decoded[0]),
        trigger_level=None,
        limit_level=None,
    )


def _first(trades: Sequence[Any], match: Callable[[Any], bool]) -> LegSnapshot | None:
    return next((_snapshot(t) for t in trades if match(t)), None)


class LegLookupMixin:
    async def get_leg(
        self: Any,
        target: str,  # noqa: ARG002  (SupportsLegLookup signature; ids are not symbol-scoped)
        *,
        client_order_id: str | None = None,
        link_tag: str | None = None,
    ) -> LegSnapshot | None:
        """Look up one leg by ``client_order_id`` or ``link_tag`` (open trades, then completed).

        If both are given, ``client_order_id`` wins (same as the Coinbase and Kraken lookups).
        ``client_order_id`` is ``str(orderId)`` and only matches orders of this connection's
        client id; ``link_tag`` matches ``orderRef`` across every client. Searches the
        ``openTrades()`` cache, then ``reqCompletedOrdersAsync(apiOnly=True)``. Returns
        ``None`` when nothing matches or no key is given (no I/O). Raises
        ``ConnectionLostError`` when disconnected.
        """
        key = client_order_id or link_tag
        if key is None:
            return None
        transport = self._transport
        if client_order_id:
            own = transport.client_id

            def match(t: Any) -> bool:
                return str(t.order.orderId) == client_order_id and t.order.clientId == own

        else:

            def match(t: Any) -> bool:
                return bool(t.order.orderRef) and t.order.orderRef == key

        found = _first(transport.open_trades(), match)
        if found is not None:
            return found
        # Unverified live (#250): completed orders may report clientId 0, which would make the
        # by-id clientId match above miss them.
        return _first(await transport.completed_trades(), match)

    async def list_open_legs(
        self: Any, target: str, *, group: str | None = None
    ) -> Sequence[LegSnapshot]:
        """Open link-tagged orders on ``target`` from the ``openTrades()`` cache.

        Includes tagged orders from any client id (so recovery works after reconnecting with a
        different client id). ``group`` keeps only legs whose ``group_id`` equals it.
        """
        symbol = (await self._resolve_native(target)).symbol
        legs = [_snapshot(t) for t in self._transport.open_trades() if t.contract.symbol == symbol]
        return [
            leg
            for leg in legs
            if leg.group_id is not None and (group is None or leg.group_id == group)
        ]
