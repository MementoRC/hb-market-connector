"""hb-market-connector: Live market connector adapter for Hummingbot."""

from market_connector.__about__ import __version__
from market_connector.live_market_access import LiveMarketAccess

__all__ = ["__version__", "LiveMarketAccess"]
