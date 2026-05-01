"""hb_compat — hummingbot bridge layer for the Kraken connector.

This package provides adapter-layer code that translates hummingbot
``ConnectorBase`` calls into ``KrakenGateway`` calls.  Gateway core code
(mixins, transport, specs) is intentionally kept free of hummingbot imports.

Public API:
    KrakenConnectorBridge  — thin ConnectorBase wrapper over KrakenGateway
    reconcile_stale_orders — startup hook: filters orphaned in-flight orders
"""

from market_connector.exchanges.kraken.hb_compat.kraken_bridge import KrakenConnectorBridge
from market_connector.exchanges.kraken.hb_compat.kraken_startup_cleanup import (
    reconcile_stale_orders,
)

__all__ = [
    "KrakenConnectorBridge",
    "reconcile_stale_orders",
]
