"""Startup stale-order reconciliation for the Kraken hb_compat bridge.

This module carries forward the semantic content of
``_for_bleed_manual/kraken-tweaks`` commit ``4c1b83f9``, which addressed the
known issue: *"Stale Kraken orders on startup: ~22 orders from previous
sessions with no exchange_order_id cause log spam"*.

The function :func:`reconcile_stale_orders` is called during connector
initialisation (from :class:`KrakenConnectorBridge.start`) — not in
``__init__`` so that startup failures don't silently swallow exceptions
during object construction.

Usage example::

    async def start(self) -> None:
        await self.gateway.start()
        await reconcile_stale_orders(self.gateway, self)

Design notes:
    - This is a hummingbot-strategy concern, NOT a gateway concern.  The
      gateway sees only fresh exchange state.
    - All exceptions are caught and logged; this hook must never raise so
      that connector initialisation always completes.
    - ``hummingbot_connector`` is typed as ``Any`` to avoid importing
      hummingbot ``ConnectorBase`` here (optional dependency).
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway

logger = logging.getLogger(__name__)


async def reconcile_stale_orders(
    gateway: KrakenGateway,
    hummingbot_connector: Any,
) -> int:
    """Remove or mark cancelled any in-flight orders that are stale on startup.

    Compares the in-flight order tracker on *hummingbot_connector* against the
    fresh open-orders snapshot from Kraken.  Three classes of stale orders are
    handled:

    1. Orders with no ``exchange_order_id`` — never submitted or lost in
       transit.  Removed from the tracker and logged at ``INFO``.
    2. Orders whose ``exchange_order_id`` is not in the Kraken open-orders
       response — cancelled, filled, or expired.  Marked as cancelled and
       logged at ``WARNING``.

    Args:
        gateway: A *started* :class:`KrakenGateway` instance.
        hummingbot_connector: A hummingbot connector that exposes an
            ``in_flight_orders`` dict (``{client_order_id: InFlightOrder}``).
            Accessed via ``getattr`` so that the function degrades gracefully
            when used outside hummingbot.

    Returns:
        Count of reconciled (removed or marked-cancelled) orders.  Returns
        ``0`` on any exception so the caller can treat it as advisory.

    Raises:
        Never — all exceptions are caught and logged.
    """
    reconciled = 0
    try:
        open_orders = await gateway.get_open_orders()
        in_flight: dict[str, Any] = getattr(hummingbot_connector, "in_flight_orders", {})

        if not in_flight:
            logger.debug("reconcile_stale_orders: in_flight_orders is empty, nothing to do.")
            return 0

        stale_no_id: list[str] = []
        stale_missing: list[str] = []

        for client_oid, order in list(in_flight.items()):
            exchange_oid: str | None = getattr(order, "exchange_order_id", None)

            if not exchange_oid:
                stale_no_id.append(client_oid)
            elif exchange_oid not in open_orders:
                stale_missing.append(client_oid)

        # Remove orders with no exchange ID
        for client_oid in stale_no_id:
            try:
                in_flight.pop(client_oid, None)
                logger.info(
                    "reconcile_stale_orders: removed order %s — no exchange_order_id.",
                    client_oid,
                )
                reconciled += 1
            except Exception:  # noqa: BLE001
                logger.exception("reconcile_stale_orders: error removing order %s.", client_oid)

        # Mark orders missing from exchange as cancelled
        for client_oid in stale_missing:
            order = in_flight.get(client_oid)
            if order is None:
                continue
            try:
                _mark_cancelled(order, client_oid)
                reconciled += 1
            except Exception:  # noqa: BLE001
                logger.exception(
                    "reconcile_stale_orders: error marking order %s as cancelled.", client_oid
                )

    except Exception:  # noqa: BLE001
        logger.exception("reconcile_stale_orders: unexpected error — startup cleanup aborted.")

    return reconciled


def _mark_cancelled(order: Any, client_oid: str) -> None:
    """Best-effort: set order state to cancelled and log a warning.

    Attempts the hummingbot ``InFlightOrder`` cancel pattern
    (``order.cancel_attempted = True`` + ``order.current_state``).
    Falls back to a plain ``status`` attribute set if the hummingbot model
    is not available.
    """
    exchange_oid = getattr(order, "exchange_order_id", "<unknown>")
    logger.warning(
        "reconcile_stale_orders: order %s (exchange_id=%s) not found in open orders — "
        "marking as cancelled.",
        client_oid,
        exchange_oid,
    )
    # Attempt hummingbot InFlightOrder state mutation
    if hasattr(order, "cancel_attempted"):
        with contextlib.suppress(AttributeError, TypeError):
            order.cancel_attempted = True
    # Fallback for duck-typed or simplified order objects
    if hasattr(order, "status"):
        with contextlib.suppress(AttributeError, TypeError):
            order.status = "cancelled"


__all__ = [
    "reconcile_stale_orders",
]
