"""LegLookupMixin: ``SupportsLegLookup`` for Interactive Brokers (#250).

Link tag: the ``Order.orderRef`` string (``mcl-<group>-<leg>``). ``client_order_id`` is
``str(order.orderId)``, unique only within one API client id. A foreign or empty ``orderRef``
is still found by ``get_leg`` (``link_tag``/``group_id`` ``None``) and excluded from
``list_open_legs``.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from data_type_primitives import OrderState

from market_connector.exchanges.interactive_brokers.exceptions import OpenOrdersSyncError
from market_connector.exchanges.interactive_brokers.linked_order_codec import IbOrderRefLinkCodec
from market_connector.exchanges.interactive_brokers.order_state import to_order_state
from market_connector.protocols import LegSnapshot

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

_CODEC = IbOrderRefLinkCodec()
logger = logging.getLogger(__name__)


def _snapshot(trade: Any, transport: Any) -> LegSnapshot:
    """Snapshot of ``trade``; ``filled`` is max(orderStatus.filled, observed execution cumQty).

    Live evidence (#326): completed orders may report ``filled == 0`` and a Cancelled/Inactive
    status although executions filled them. Reaching ``totalQuantity`` yields FILLED.
    """
    order, status = trade.order, trade.orderStatus
    ref = order.orderRef
    decoded = _CODEC.decode(ref) if ref else None
    filled = Decimal(str(status.filled)) if status.filled is not None else Decimal(0)
    observed = transport.observed_cum_qty(order.permId, ref)
    if observed is not None and observed > filled:
        filled = observed
    total = Decimal(str(getattr(order, "totalQuantity", 0) or 0))
    if total > 0 and filled >= total:
        state = OrderState.FILLED
    else:
        state = to_order_state(status.status, filled)
    return LegSnapshot(
        client_order_id=str(order.orderId),
        link_tag=None if decoded is None else ref,
        state=state,
        filled=filled,
        venue_order_id=str(order.permId) if order.permId else None,
        group_id=None if decoded is None else str(decoded[0]),
        trigger_level=None,
        limit_level=None,
    )


def _first(
    trades: Sequence[Any], match: Callable[[Any], bool], transport: Any
) -> LegSnapshot | None:
    return next((_snapshot(t, transport) for t in trades if match(t)), None)


def _in_scope(trade: Any, native: Any, account: str | None) -> bool:
    """True when ``trade`` is on the target's contract and (if configured) this account.

    Contract: the symbol must match and, when both sides carry a ``conId``, so must the
    ``conId`` (completed orders may lack one, so the symbol alone is then used).
    """
    contract = trade.contract
    if contract.symbol != native.symbol:
        return False
    con_id, want = getattr(contract, "conId", 0), getattr(native, "conId", 0)
    if con_id and want and con_id != want:
        return False
    return not account or trade.order.account == account


async def _refresh_open_orders(transport: Any) -> None:
    """Refresh the all-clients open-order snapshot via the transport's existing sync.

    No-op when ``fetch_all_open_orders`` is off or the connection is read-only. A failed or
    timed-out sync keeps the cache as is (stale rather than raising); a lost connection
    still raises ``ConnectionLostError``.
    """
    spec = transport._spec
    if spec.readonly or not spec.fetch_all_open_orders:
        return
    try:
        await transport.sync_open_orders(timeout=spec.connect_timeout or 15.0)
    except OpenOrdersSyncError as exc:
        logger.warning("IB all-open-orders refresh failed; using cached open orders: %s", exc)


class LegLookupMixin:
    async def get_leg(
        self: Any,
        target: str,
        *,
        client_order_id: str | None = None,
        link_tag: str | None = None,
    ) -> LegSnapshot | None:
        """Look up one leg by ``client_order_id`` or ``link_tag`` (open trades, then completed).

        If both are given, ``client_order_id`` wins (same as the Coinbase and Kraken lookups).
        ``client_order_id`` is ``str(orderId)`` and only matches orders of this connection's
        client id; ``link_tag`` matches ``orderRef`` across every client. Only trades on
        ``target``'s contract (and the configured account, if any) are returned. Searches the
        ``openTrades()`` cache, then ``reqCompletedOrdersAsync(apiOnly=True)``. Returns
        ``None`` when nothing matches or no key is given (no I/O). Raises
        ``ConnectionLostError`` when disconnected.

        Cross-client visibility (other clients' orders, link-tag path) requires
        ``fetch_all_open_orders`` and a non-read-only connection; the link-tag path refreshes
        the open-order snapshot first. Otherwise only this client's orders are seen.
        """
        key = client_order_id or link_tag
        if not key:
            return None
        transport = self._transport
        if client_order_id:
            own = transport.client_id

            def match(t: Any) -> bool:
                return str(t.order.orderId) == client_order_id and t.order.clientId == own

        else:
            await _refresh_open_orders(transport)

            def match(t: Any) -> bool:
                return bool(t.order.orderRef) and t.order.orderRef == key

        open_trades = transport.open_trades()
        native = await self._resolve_native(target)
        account = transport.account

        def scoped(t: Any) -> bool:
            return match(t) and _in_scope(t, native, account)

        found = _first(open_trades, scoped, transport)
        if found is not None:
            return found
        # Live probe (#250, IB paper Gateway): completed orders carry orderId=0/clientId=0, so
        # they are matched by the permId recorded while the order was open (in-process only).
        # apiOnly=True returned other clients' completed orders identically to apiOnly=False
        # (4/4 trades). orderRef survival on completed orders is still unverified.
        if client_order_id:
            perm = transport.perm_id_for(client_order_id)

            def match_perm(t: Any) -> bool:
                return perm is not None and t.order.permId == perm and _in_scope(t, native, account)

            found = _first(await transport.completed_trades(), match_perm, transport)
            return None if found is None else replace(found, client_order_id=client_order_id)
        return _first(await transport.completed_trades(), scoped, transport)

    async def list_open_legs(
        self: Any, target: str, *, group: str | None = None
    ) -> Sequence[LegSnapshot]:
        """Open link-tagged orders on ``target`` from the ``openTrades()`` cache.

        Includes tagged orders from any client id (so recovery works after reconnecting with a
        different client id), limited to ``target``'s contract and the configured account (if
        any). ``group`` keeps only legs whose ``group_id`` equals it.

        Cross-client visibility requires ``fetch_all_open_orders`` and a non-read-only
        connection; the open-order snapshot is refreshed before reading. Otherwise only this
        client's orders are listed.
        """
        native = await self._resolve_native(target)
        await _refresh_open_orders(self._transport)
        account = self._transport.account
        legs = [
            _snapshot(t, self._transport)
            for t in self._transport.open_trades()
            if _in_scope(t, native, account)
        ]
        return [
            leg
            for leg in legs
            if leg.group_id is not None and (group is None or leg.group_id == group)
        ]
