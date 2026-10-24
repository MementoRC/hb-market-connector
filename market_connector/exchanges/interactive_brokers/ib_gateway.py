"""IbGatewayGateway: composition root for the IB integration.

Stage 1 shell: holds transport + signer; ready property reflects transport
connection state. Stage 2-6 will progressively add mixins (Orders, Accounts,
MarketData, Subscriptions) and the contract_resolver.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from decimal import Decimal

    from market_connector.auth.passthrough import PassThroughSigner
    from market_connector.contracts.instrument import InstrumentRef
    from market_connector.contracts.protocols import ContractResolver
    from market_connector.exchanges.interactive_brokers.order_handle import OrderHandle
    from market_connector.exchanges.interactive_brokers.positions import IbPosition
    from market_connector.exchanges.interactive_brokers.transport import (
        ConnectionStateEvent,
        FeeEvent,
        FillEvent,
        IbGatewayTransport,
        SubscriptionHandle,
    )
    from market_connector.exchanges.interactive_brokers.what_if import IbWhatIfResult
    from market_connector.orders import HBOrder
    from market_connector.transport.protocols import (
        RequestTransport,
        StreamTransport,
    )

from market_connector.exchanges.interactive_brokers.mixins import (
    MarketDataMixin,
    SubscriptionsMixin,
)
from market_connector.exchanges.interactive_brokers.trading_rules import IbTradingRules


class IbGatewayGateway(MarketDataMixin, SubscriptionsMixin):
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
        self._tick_counter: dict[tuple[int, int], int] = {}

    @property
    def unified_transport(self) -> IbGatewayTransport | None:
        """Protocol slot: exposes the concrete transport as Transport | None."""
        return self._transport

    @property
    def _contract_resolver(self) -> ContractResolver:
        """Alias to self.contract_resolver for mixin protocol conformance.

        Raises RuntimeError if contract_resolver was not set by the factory.
        """
        if self.contract_resolver is None:
            raise RuntimeError(
                "IbGatewayGateway has no contract_resolver; build it with build_ib_gateway() "
                "or pass contract_resolver="
            )
        return self.contract_resolver

    # --- Lifecycle (Stage 1) ---

    @property
    def ready(self) -> bool:
        return self._transport.is_connected

    async def start(self) -> None:
        await self._transport.connect()

    async def stop(self) -> None:
        await self._transport.disconnect()

    def subscribe_connection_state(
        self, callback: Callable[[ConnectionStateEvent], None]
    ) -> SubscriptionHandle:
        """Delegate to transport: observe connected/disconnected/reconnecting/data_lost."""
        return self._transport.subscribe_connection_state(callback)

    def subscribe_fills(self, callback: Callable[[FillEvent], None]) -> SubscriptionHandle:
        """Delegate to transport: observe executions (live and reconciled)."""
        return self._transport.subscribe_fills(callback)

    def subscribe_fees(self, callback: Callable[[FeeEvent], None]) -> SubscriptionHandle:
        """Delegate to transport: observe execution fees (live and reconciled)."""
        return self._transport.subscribe_fees(callback)

    # --- ExecutionGateway (Stage 2) ---

    async def place_order(self, ref: InstrumentRef, hb_order: HBOrder) -> OrderHandle:
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

    async def what_if(self, ref: InstrumentRef, hb_order: HBOrder) -> IbWhatIfResult:
        """Resolve contract then preview the order's margin and commission impact.

        Places nothing. Raises RuntimeError if no contract_resolver has been wired.
        """
        if self.contract_resolver is None:
            raise RuntimeError(
                "Gateway has no contract_resolver; cannot run what-if. "
                "Use build_ib_gateway() to construct a fully-wired gateway."
            )
        resolved = await self.contract_resolver.resolve(ref)
        return await self._transport.what_if(resolved.native, hb_order)

    async def get_trading_rules(self, ref: InstrumentRef) -> IbTradingRules:
        """Resolve contract (cached) and derive trading rules from its details.

        Raises RuntimeError if no contract_resolver has been wired, and
        ContractNotFoundError if the instrument cannot be resolved.
        """
        if self.contract_resolver is None:
            raise RuntimeError(
                "Gateway has no contract_resolver; cannot get trading rules. "
                "Use build_ib_gateway() to construct a fully-wired gateway."
            )
        resolved = await self.contract_resolver.resolve(ref)
        return IbTradingRules.from_contract_details(resolved.metadata["contract_details"])

    async def cancel_order(self, handle: OrderHandle) -> OrderHandle:
        """Delegate cancel to transport (idempotent on terminal orders)."""
        return await self._transport.cancel_order(handle)

    async def cancel_all(self) -> list[OrderHandle]:
        """Delegate to transport: cancel this client's open orders (not manual TWS ones).

        Raises CancelAllError (ExceptionGroup with ``.cancelled``) if any cancel fails.
        """
        return await self._transport.cancel_all()

    def get_open_orders(self) -> list[OrderHandle]:
        """Return a snapshot of open orders from the transport's local cache."""
        return self._transport.open_orders()

    async def get_balance(self, currency: str) -> Decimal:
        """Delegate to transport: available balance of *currency* for the resolved account."""
        return await self._transport.get_balance(currency)

    async def get_positions(self) -> list[IbPosition]:
        """Delegate to transport: open positions of the resolved account."""
        return await self._transport.get_positions()
