"""Kraken-specific REST transport layer.

Kraken's REST API uses form-encoded POST bodies (application/x-www-form-urlencoded)
and HMAC-SHA512 authentication.  Both are handled declaratively by the framework:

- Body encoding: ``BodyFormat.FORM_URLENCODED`` in ``KRAKEN_HMAC_SPEC`` (Stage 1)
- Authentication: ``DeclarativeRestSigner`` driven by ``KRAKEN_HMAC_SPEC``

There are no Kraken-specific transport overrides required.  This module exports
``KrakenTransport`` as a no-op marker class for consistency with the Coinbase
exchange layout and to provide a stable import target for Stage 4 mixins.
"""

from market_connector.transport.rest_base import RestConnectorBase


class KrakenTransport(RestConnectorBase):
    """RestConnectorBase subclass for the Kraken REST API.

    No overrides are required: body encoding and signing are handled by the
    framework-level ``BodyFormat.FORM_URLENCODED`` spec and ``DeclarativeRestSigner``.
    Subclasses or Stage 4 mixins may add exchange-specific behaviour here.
    """
