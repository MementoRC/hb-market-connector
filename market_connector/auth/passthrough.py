"""No-op signer for exchanges with process-managed authentication.

Used by exchanges where the local socket carries authenticated state
implicitly (e.g., IB Gateway holds the session after interactive 2FA login,
the local Python client connects to localhost without credentials).

Structurally satisfies the Signer Protocol (market_connector/auth/protocols.py)
without inheriting from it -- Signer is a Protocol, not an ABC.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from market_connector.auth.protocols import Request


class PassThroughSigner:
    """No-op Signer implementation. sign() returns the request unchanged."""

    async def sign(self, request: Request) -> Request:
        return request
