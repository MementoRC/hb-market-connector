"""Exchange-specific connectors implementing market_connector protocols.

Each exchange lives under its own subpackage (e.g. ``market_connector.exchanges.coinbase``)
and inherits from the framework's transport base classes
(``RestConnectorBase``, ``WsConnectorBase``) and primitives.

Install runtime dependencies for a specific exchange via optional extras::

    pip install hb-market-connector[coinbase]
"""
