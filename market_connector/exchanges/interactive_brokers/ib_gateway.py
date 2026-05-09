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
    from market_connector.contracts.instrument import InstrumentRef
    from market_connector.contracts.protocols import ContractResolver
    from market_connector.exchanges.interactive_brokers.order_handle import OrderHandle
    from market_connector.exchanges.interactive_brokers.transport import IbGatewayTransport
    from market_connector.orders import HBOrder
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

    # --- ExecutionGateway (Stage 2) ---

    async def place_order(
        self, ref: "InstrumentRef", hb_order: "HBOrder"
    ) -> "OrderHandle":
        """Resolve contract then delegate to transport.place_order.

        The gateway is responsible for contract resolution so that the transport
        remains free of resolver dependencies. Raises RuntimeError if no
        contract_resolver has been wired (Stage 1 condition; should not occur
        in Stage 2+ when factory is used).
        """
        if self.contract_resolver is None:
            raise RuntimeError(
                "Gateway has no contract_resolver; cannot place orders. "
                "Use build_ib_gateway() to construct a fully-wired gateway."
            )
        resolved = await self.contract_resolver.resolve(ref)
        return await self._transport.place_order(resolved.native, hb_order)

    async def cancel_order(self, handle: "OrderHandle") -> "OrderHandle":
        """Delegate cancel to transport (idempotent on terminal orders)."""
        return await self._transport.cancel_order(handle)

    def get_open_orders(self) -> "list[OrderHandle]":
        """Return a snapshot of open orders from the transport's local cache."""
        return self._transport.open_orders()

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
