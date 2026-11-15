"""IbGatewayGateway: composition root for the IB integration.

Stage 1 shell: holds transport + signer; ready property reflects transport
connection state. Stage 2-6 will progressively add mixins (Orders, Accounts,
MarketData, Subscriptions) and the contract_resolver.
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from datetime import datetime
    from decimal import Decimal

    from market_connector.auth.passthrough import PassThroughSigner
    from market_connector.contracts.instrument import InstrumentRef
    from market_connector.contracts.protocols import ContractResolver
    from market_connector.exchanges.interactive_brokers.linked_orders import IbBracket, IbOcaGroup
    from market_connector.exchanges.interactive_brokers.order_handle import OrderHandle
    from market_connector.exchanges.interactive_brokers.order_options import IbOrderOptions
    from market_connector.exchanges.interactive_brokers.pnl import IbPnL, IbPositionPnL
    from market_connector.exchanges.interactive_brokers.positions import IbPosition
    from market_connector.exchanges.interactive_brokers.transport import (
        ConnectionStateEvent,
        FeeEvent,
        FillEvent,
        IbAccountSummary,
        IbGatewayTransport,
        IbServerInfo,
        SubscriptionHandle,
    )
    from market_connector.exchanges.interactive_brokers.what_if import IbWhatIfResult
    from market_connector.transport.protocols import (
        RequestTransport,
        StreamTransport,
    )

from data_type_primitives import (
    LegRole,
    LegSpec,
    LinkedOrderSpec,
    TriggerKind,
    TriggerReference,
)
from data_type_primitives.common import OrderType

from market_connector.capabilities import LinkedMode, OrderCapabilities
from market_connector.exceptions import GatewayNotStartedError
from market_connector.exchanges.interactive_brokers.linked_orders import OcaType
from market_connector.exchanges.interactive_brokers.mixins import (
    MarketDataMixin,
    SubscriptionsMixin,
)
from market_connector.exchanges.interactive_brokers.trading_rules import IbTradingRules
from market_connector.hb_compat.primitives import coerce_order_type, coerce_trade_type
from market_connector.orders import HBOrder


def _leg_to_order(leg: LegSpec) -> HBOrder:
    """Map a venue-neutral ``LegSpec`` to the HBOrder the IB transport consumes."""
    return HBOrder(
        order_type=coerce_order_type(leg.order_type),
        side=coerce_trade_type(leg.trade_type),
        amount=leg.amount,
        price=leg.price,
        trigger=leg.trigger,
    )


# Must match the types ``_hb_to_ib_order`` (transport.py) accepts; enforced by the capability
# contract test.
IB_ORDER_CAPABILITIES = OrderCapabilities(
    order_types=frozenset(
        {OrderType.MARKET, OrderType.LIMIT, OrderType.STOP_LOSS, OrderType.STOP_LOSS_LIMIT}
    ),
    trigger_kinds=frozenset({TriggerKind.ABSOLUTE}),
    trigger_references=frozenset({TriggerReference.LAST}),
    supports_trailing=False,
    linked=frozenset({LinkedMode.BRACKET, LinkedMode.OCO}),
    native_linked=True,
)


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
    def order_capabilities(self) -> OrderCapabilities:
        """Order features this gateway supports (see ``HasCapabilities``)."""
        return IB_ORDER_CAPABILITIES

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

    @property
    def server_info(self) -> IbServerInfo | None:
        """Delegate to transport: negotiated server version, None before the first connect."""
        return self._transport.server_info

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

    async def place_order(
        self,
        ref: InstrumentRef,
        hb_order: HBOrder,
        *,
        options: IbOrderOptions | None = None,
    ) -> OrderHandle:
        """Resolve contract then delegate to transport.place_order.

        ``options`` carries opt-in IB-specific settings (e.g. the Adaptive algo);
        an invalid combination raises ValueError before any IB call.

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
        return await self._transport.place_order(resolved.native, hb_order, options=options)

    async def _resolve_native(self, target: str | InstrumentRef) -> Any:
        """Resolve a pair string or InstrumentRef to the native contract (see dispatch.py)."""
        if self.contract_resolver is None:
            raise GatewayNotStartedError(
                "Gateway has no contract_resolver; cannot place orders. "
                "Use build_ib_gateway() to construct a fully-wired gateway."
            )
        if isinstance(target, str):
            return (await self.contract_resolver.resolve_from_pair(target)).native
        return (await self.contract_resolver.resolve(target)).native

    async def place_bracket(
        self,
        target: str | InstrumentRef,
        spec: LinkedOrderSpec | HBOrder | None = None,
        take_profit: HBOrder | None = None,
        stop_loss: HBOrder | None = None,
        *,
        options: Mapping[str, Any] | None = None,  # noqa: ARG002  (SupportsBracket signature)
        entry: HBOrder | None = None,
    ) -> IbBracket:
        """Resolve contract once then place an entry with take-profit and/or stop-loss children.

        Preferred form: ``place_bracket(target, spec)`` with a BRACKET ``LinkedOrderSpec``; one
        or two exits. The legacy per-leg form ``(ref, entry, take_profit, stop_loss)`` still
        works but emits a DeprecationWarning.

        Raises GatewayNotStartedError if no contract_resolver has been wired.
        """
        if isinstance(spec, LinkedOrderSpec):
            entry_leg = spec.entry
            entry_order = None if entry_leg is None else _leg_to_order(entry_leg)
            exits = {leg.role: _leg_to_order(leg) for leg in spec.exits}
            take_profit = exits.get(LegRole.TAKE_PROFIT)
            stop_loss = exits.get(LegRole.STOP_LOSS)
        else:
            warnings.warn(
                "place_bracket(ref, entry, take_profit, stop_loss) is deprecated; "
                "pass a LinkedOrderSpec: place_bracket(target, spec)",
                DeprecationWarning,
                stacklevel=2,
            )
            entry_order = spec if spec is not None else entry
        if entry_order is None:
            raise TypeError("place_bracket requires a LinkedOrderSpec or an entry order")
        native = await self._resolve_native(target)
        return await self._transport.place_bracket(native, entry_order, take_profit, stop_loss)

    async def place_oco(
        self,
        target: str | InstrumentRef,
        spec: LinkedOrderSpec,
        *,
        options: Mapping[str, Any] | None = None,
    ) -> IbOcaGroup:
        """Place an OCO ``LinkedOrderSpec`` as one IB OCA group, legs in ``ordered_legs()`` order.

        ``options={"oca_type": OcaType.X}`` selects IB's OCA behaviour (default
        CANCEL_WITH_BLOCK).

        Raises GatewayNotStartedError if no contract_resolver has been wired.
        """
        oca_type = (options or {}).get("oca_type", OcaType.CANCEL_WITH_BLOCK)
        native = await self._resolve_native(target)
        orders = [_leg_to_order(leg) for leg in spec.ordered_legs()]
        return await self._transport.place_oca(native, orders, oca_type)

    async def place_oca(
        self,
        ref: InstrumentRef,
        orders: Sequence[HBOrder],
        oca_type: OcaType = OcaType.CANCEL_WITH_BLOCK,
    ) -> IbOcaGroup:
        """Resolve contract once then place *orders* as one OCA group.

        Raises GatewayNotStartedError if no contract_resolver has been wired.
        """
        if self.contract_resolver is None:
            raise GatewayNotStartedError(
                "Gateway has no contract_resolver; cannot place orders. "
                "Use build_ib_gateway() to construct a fully-wired gateway."
            )
        resolved = await self.contract_resolver.resolve(ref)
        return await self._transport.place_oca(resolved.native, orders, oca_type)

    async def what_if(
        self,
        ref: InstrumentRef,
        hb_order: HBOrder,
        *,
        options: IbOrderOptions | None = None,
    ) -> IbWhatIfResult:
        """Resolve contract then preview the order's margin and commission impact.

        ``options`` are previewed as place_order would send them. Places nothing.
        Raises RuntimeError if no contract_resolver has been wired.
        """
        if self.contract_resolver is None:
            raise RuntimeError(
                "Gateway has no contract_resolver; cannot run what-if. "
                "Use build_ib_gateway() to construct a fully-wired gateway."
            )
        resolved = await self.contract_resolver.resolve(ref)
        return await self._transport.what_if(resolved.native, hb_order, options=options)

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

    async def get_account_summary(self, currency: str | None = None) -> IbAccountSummary:
        """Delegate to transport: account summary in *currency* (None = BASE)."""
        return await self._transport.get_account_summary(currency)

    async def get_fills(self, since: datetime) -> list[tuple[FillEvent, FeeEvent | None]]:
        """Delegate to transport: executions since *since* (aware) with fees, oldest first.

        IB keeps roughly 7 days; the fee is None until its commission report arrives.
        """
        return await self._transport.get_fills(since)

    def managed_accounts(self) -> list[str]:
        """Delegate to transport: accounts the Gateway session is logged into.

        Raises ManagedAccountsUnavailableError when IB reports none.
        """
        return self._transport.managed_accounts()

    async def sync_open_orders(self, timeout: float) -> None:
        """Delegate to transport: on-demand all-clients open-order sync within *timeout* seconds.

        Raises OpenOrdersSyncTimeoutError or OpenOrdersSyncError on failure.
        """
        await self._transport.sync_open_orders(timeout=timeout)

    async def get_positions(self) -> list[IbPosition]:
        """Delegate to transport: open positions of the resolved account."""
        return await self._transport.get_positions()

    async def get_pnl(self) -> IbPnL:
        """Delegate to transport: account-level PnL snapshot (first answer may be stale)."""
        return await self._transport.get_pnl()

    async def get_position_pnl(self, ref: InstrumentRef) -> IbPositionPnL:
        """Resolve contract then fetch the PnL snapshot of that position.

        Raises RuntimeError if no contract_resolver has been wired.
        """
        if self.contract_resolver is None:
            raise RuntimeError(
                "Gateway has no contract_resolver; cannot get position PnL. "
                "Use build_ib_gateway() to construct a fully-wired gateway."
            )
        resolved = await self.contract_resolver.resolve(ref)
        return await self._transport.get_position_pnl(resolved.native.conId)

    def subscribe_pnl(self, callback: Callable[[IbPnL], None]) -> SubscriptionHandle:
        """Delegate to transport: stream account-level PnL updates to *callback*."""
        return self._transport.subscribe_pnl(callback)

    async def subscribe_position_pnl(
        self, ref: InstrumentRef, callback: Callable[[IbPositionPnL], None]
    ) -> SubscriptionHandle:
        """Resolve contract then stream that position's PnL updates to *callback*.

        Raises RuntimeError if no contract_resolver has been wired.
        """
        if self.contract_resolver is None:
            raise RuntimeError(
                "Gateway has no contract_resolver; cannot subscribe to position PnL. "
                "Use build_ib_gateway() to construct a fully-wired gateway."
            )
        resolved = await self.contract_resolver.resolve(ref)
        return self._transport.subscribe_position_pnl(resolved.native.conId, callback)
