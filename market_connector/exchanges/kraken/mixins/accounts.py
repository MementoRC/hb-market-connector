"""AccountsMixin: balance and WebSocket token retrieval via Kraken REST API."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from market_connector.exceptions import GatewayNotStartedError
from market_connector.exchanges.kraken.errors import raise_on_kraken_error
from market_connector.exchanges.kraken.schemas.rest import (
    BalanceResult,
    KrakenResponse,
    WebSocketsTokenResult,
)

if TYPE_CHECKING:
    from market_connector.exchanges.kraken.mixins.protocols import HasReady, HasRest


class AccountsMixin:
    async def get_balance(self: HasRest & HasReady, currency: str) -> Decimal:  # type: ignore[valid-type]
        """Return the balance for *currency*, or Decimal("0") if not found.

        Args:
            currency: Raw Kraken asset code (e.g. ``"XXBT"``, ``"ZUSD"``).

        Returns:
            Decimal balance, or ``Decimal("0")`` if the asset is not present.

        Raises:
            GatewayNotStartedError: If the gateway has not been started.
        """
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")

        response = await self._rest.request("balance")
        parsed: KrakenResponse[BalanceResult] = KrakenResponse[BalanceResult].model_validate(
            response.raw
        )
        raise_on_kraken_error(parsed.error)
        result = parsed.result or {}
        return Decimal(result[currency]) if currency in result else Decimal("0")

    async def get_balances(self: HasRest & HasReady) -> dict[str, Decimal]:  # type: ignore[valid-type]
        """Return the full balance snapshot as a dict of asset code → Decimal.

        Kraken asset codes are returned as-is (e.g. ``"XXBT"``, ``"ZUSD"``).
        Canonicalization to exchange-neutral symbols is handled in Stage 5 hb_compat.

        Raises:
            GatewayNotStartedError: If the gateway has not been started.
        """
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")

        response = await self._rest.request("balance")
        parsed: KrakenResponse[BalanceResult] = KrakenResponse[BalanceResult].model_validate(
            response.raw
        )
        raise_on_kraken_error(parsed.error)
        result = parsed.result or {}
        return {asset: Decimal(amount) for asset, amount in result.items()}

    async def get_websockets_token(self: HasRest & HasReady) -> tuple[str, int]:  # type: ignore[valid-type]
        """Fetch a short-lived WebSocket authentication token from Kraken.

        Returns:
            A ``(token, expires_in_seconds)`` tuple.  The token is injected
            into private WebSocket subscribe payloads by ``TokenFetchAuth``.

        Raises:
            GatewayNotStartedError: If the gateway has not been started.
        """
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")

        response = await self._rest.request("get_websockets_token")
        parsed: KrakenResponse[WebSocketsTokenResult] = KrakenResponse[
            WebSocketsTokenResult
        ].model_validate(response.raw)
        raise_on_kraken_error(parsed.error)
        assert parsed.result is not None  # guarded by raise_on_kraken_error above
        return parsed.result.token, parsed.result.expires
