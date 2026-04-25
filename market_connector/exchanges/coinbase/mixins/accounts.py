"""AccountsMixin: balance retrieval via Coinbase REST API."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from market_connector.exceptions import GatewayNotStartedError
from market_connector.exchanges.coinbase.converters import to_balance
from market_connector.exchanges.coinbase.schemas.rest import ListAccountsResponse

if TYPE_CHECKING:
    from market_connector.exchanges.coinbase.mixins.protocols import HasReady, HasRest


class AccountsMixin:
    async def get_balance(self: HasRest & HasReady, currency: str) -> Decimal:  # type: ignore[valid-type]
        """Return the available balance for *currency*, or Decimal("0") if not found."""
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")

        raw = await self._rest.request("accounts")
        response = ListAccountsResponse.model_validate(raw)
        for account in response.accounts:
            if account.currency == currency:
                return to_balance(account)
        return Decimal("0")
