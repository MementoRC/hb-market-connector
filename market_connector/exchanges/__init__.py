"""Exchange-specific connectors implementing market_connector protocols.

Each exchange lives under its own subpackage (e.g. ``market_connector.exchanges.coinbase``)
and inherits from the framework's transport base classes
(``RestConnectorBase``, ``WsConnectorBase``) and primitives.

Runtime dependencies for a specific exchange come from conda-forge through the
pixi feature of the same name (e.g. ``coinbase``, ``kraken``, ``interactive-brokers``)::

    pixi run -e coinbase test
"""
