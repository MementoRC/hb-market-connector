"""IbGatewayGateway: composition root for the IB integration.

Stage 1 shell: holds transport + signer; ready property reflects transport
connection state. Stage 2-6 will progressively add mixins (Orders, Accounts,
MarketData, Subscriptions) and the contract_resolver.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from decimal import Decimal

    from market_connector.auth.passthrough import PassThroughSigner
    from market_connector.contracts.protocols import ContractResolver
    from market_connector.exchanges.interactive_brokers.transport import IbGatewayTransport
    from market_connector.transport.protocols import (
        RequestTransport,
        StreamTransport,
        Transport,
    )


class IbGatewayGateway:
    """Composition root for IB integration.

    Conforms structurally to TransportAwareGateway (which extends ExchangeGateway).
    Stage 1 stubs: execution and market-data methods raise NotImplementedError
    pointing to the stage that will implement them.
    """

    def __init__(
        self,
        *,
        transport: IbGatewayTransport,
        signer: PassThroughSigner,
        contract_resolver: ContractResolver | None = None,
    ) -> None:
        self.signer = signer
        # Stored as concrete type so lifecycle methods (is_connected/connect/disconnect)
        # are accessible without casting. The TransportAwareGateway protocol slot
        # unified_transport: Transport | None is satisfied structurally at runtime.
        self._transport: IbGatewayTransport = transport
        self.rest_transport: RequestTransport | None = None
        self.stream_transport: StreamTransport | None = None
        self.contract_resolver: ContractResolver | None = contract_resolver

    @property
    def unified_transport(self) -> Transport | None:
        """Protocol slot: exposes the concrete transport as Transport | None."""
        return self._transport

    # --- Lifecycle (Stage 1) ---

    @property
    def ready(self) -> bool:
        return self._transport.is_connected

    async def start(self) -> None:
        await self._transport.connect()

    async def stop(self) -> None:
        await self._transport.disconnect()

    # --- ExecutionGateway stubs (Stage 2) ---

    async def place_order(
        self,
        trading_pair: str,
        order_type: str,
        side: str,
        amount: Decimal,
        price: Decimal | None,
    ) -> str:
        raise NotImplementedError("place_order() is implemented in Stage 2")

    async def cancel_order(self, trading_pair: str, client_order_id: str) -> bool:
        raise NotImplementedError("cancel_order() is implemented in Stage 2")

    async def get_open_orders(self, trading_pair: str) -> list[Any]:
        raise NotImplementedError("get_open_orders() is implemented in Stage 2")

    async def get_balance(self, currency: str) -> Decimal:
        raise NotImplementedError("get_balance() is implemented in Stage 2")

    # --- MarketDataGateway stubs (Stage 3) ---

    async def get_orderbook(self, trading_pair: str) -> Any:
        raise NotImplementedError("get_orderbook() is implemented in Stage 3")

    async def get_candles(self, trading_pair: str, interval: str, limit: int) -> list[Any]:
        raise NotImplementedError("get_candles() is implemented in Stage 3")

    async def get_mid_price(self, trading_pair: str) -> Decimal:
        raise NotImplementedError("get_mid_price() is implemented in Stage 3")

    @asynccontextmanager
    async def subscribe_orderbook(
        self,
        trading_pair: str,
        callback: Callable[[Any], None],
    ) -> AsyncIterator[None]:
        raise NotImplementedError("subscribe_orderbook() is implemented in Stage 3")
        yield  # pragma: no cover

    @asynccontextmanager
    async def subscribe_trades(
        self,
        trading_pair: str,
        callback: Callable[[Any], None],
    ) -> AsyncIterator[None]:
        raise NotImplementedError("subscribe_trades() is implemented in Stage 3")
        yield  # pragma: no cover
