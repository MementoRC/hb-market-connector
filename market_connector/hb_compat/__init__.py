"""hb_compat: isolates hummingbot/hb-* sub-package imports from the rest of
market_connector (see ADR 0001 Groups A/D).
"""

from market_connector.hb_compat.bridge import LiveMarketAccess
from market_connector.hb_compat.logging import get_logger

__all__ = ["LiveMarketAccess", "get_logger"]
