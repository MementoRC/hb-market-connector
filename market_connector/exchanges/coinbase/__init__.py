"""Coinbase Advanced Trade gateway for hb-market-connector.

Exchange-specific implementation of the market_connector transport
contracts (``RestConnectorBase``, ``WsConnectorBase``) for Coinbase's
Advanced Trade REST + WebSocket API. Install runtime dependencies via
the ``[coinbase]`` optional extra.
"""

from market_connector.exchanges.coinbase.__about__ import __version__
from market_connector.exchanges.coinbase.coinbase_gateway import CoinbaseGateway
from market_connector.exchanges.coinbase.config import CoinbaseConfig

__all__ = ["CoinbaseConfig", "CoinbaseGateway", "__version__"]
