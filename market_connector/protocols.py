"""Runtime-checkable gateway protocols for the exchange gateway framework.

Connectors implement ExchangeGateway. Consumers depend on the protocol,
never on a concrete connector class.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence
    from contextlib import AbstractAsyncContextManager as AsyncContextManager
    from decimal import Decimal

    from data_type_primitives import LinkedCancelOutcome, LinkedOrderSpec, OrderState

    from market_connector.capabilities import OrderCapabilities
    from market_connector.contracts.instrument import InstrumentRef
    from market_connector.contracts.protocols import ContractResolver
    from market_connector.hb_compat.primitives import CanonicalOrderType, CanonicalTradeType
    from market_connector.primitives import (
        CandleData,
        OpenOrder,
        OrderBookSnapshot,
        OrderBookUpdate,
        OrderType,
        TradeEvent,
        TradeType,
    )
    from market_connector.transport.protocols import (
        RequestTransport,
        StreamTransport,
        Transport,
    )

# Note: @runtime_checkable isinstance checks verify method *names* only,
# not parameter types or return types. Use mypy for full type checking.


@runtime_checkable
class HasCapabilities(Protocol):
    """Gateway that declares the order features it supports."""

    @property
    def order_capabilities(self) -> OrderCapabilities: ...


@runtime_checkable
class BracketHandle(Protocol):
    """Read-only view of a placed bracket (entry plus one or two exits)."""

    @property
    def group_id(self) -> str | int: ...

    @property
    def entry_client_id(self) -> str | int: ...

    @property
    def take_profit_client_id(self) -> str | int | None: ...

    @property
    def stop_loss_client_id(self) -> str | int | None: ...

    @property
    def emulated(self) -> bool: ...


@runtime_checkable
class OcoHandle(Protocol):
    """Read-only view of a placed OCO group; ids follow ``LinkedOrderSpec.ordered_legs()``."""

    @property
    def group_id(self) -> str | int: ...

    @property
    def leg_client_ids(self) -> tuple[str | int, ...]: ...

    @property
    def emulated(self) -> bool: ...


@runtime_checkable
class SupportsBracket(Protocol):
    """Gateway that places bracket orders (entry with take-profit and/or stop-loss)."""

    async def place_bracket(
        self,
        target: str | InstrumentRef,
        spec: LinkedOrderSpec,
        *,
        options: Mapping[str, Any] | None = None,
    ) -> BracketHandle: ...

    async def cancel_linked(self, handle: BracketHandle) -> LinkedCancelOutcome: ...


@dataclass(frozen=True)
class LegSnapshot:
    """Point-in-time state of one leg of a linked order, found by id or link tag.

    ``venue_order_id`` is the exchange's own order id (lets callers reconcile by it);
    ``group_id`` is the link group the leg belongs to, when it can be decoded.
    """

    client_order_id: str
    link_tag: str | None
    state: OrderState
    filled: Decimal
    venue_order_id: str | None = None
    group_id: str | None = None


@runtime_checkable
class SupportsLegLookup(Protocol):
    """Gateway that finds legs of linked orders by client id or link tag."""

    async def get_leg(
        self,
        target: str,
        *,
        client_order_id: str | None = None,
        link_tag: str | None = None,
    ) -> LegSnapshot | None: ...

    async def list_open_legs(
        self, target: str, *, group: str | None = None
    ) -> Sequence[LegSnapshot]:
        """Open legs on ``target``; with ``group``, only legs of that link group."""
        ...


@dataclass(frozen=True)
class LegFill:
    """One fill of a leg, with the leg's running total.

    ``cumulative_filled`` is the leg's total base volume filled so far (strictly increasing
    per ``client_order_id``); ``timestamp`` is unix seconds; ``fill_id`` is the venue trade
    id when there is one.
    """

    client_order_id: str
    cumulative_filled: Decimal
    last_price: Decimal
    timestamp: float
    fill_id: str | None = None


@runtime_checkable
class SupportsLegFills(Protocol):
    """Gateway that pushes per-leg cumulative fills to subscribers."""

    def subscribe_leg_fills(
        self, callback: Callable[[LegFill], Awaitable[None] | None]
    ) -> Callable[[], None]:
        """Register ``callback`` (sync or async); returns an idempotent unsubscribe."""
        ...


@runtime_checkable
class SupportsOco(Protocol):
    """Gateway that places OCO (one-cancels-other) groups."""

    async def place_oco(
        self,
        target: str | InstrumentRef,
        spec: LinkedOrderSpec,
        *,
        options: Mapping[str, Any] | None = None,
    ) -> OcoHandle: ...

    async def cancel_linked(self, handle: OcoHandle) -> LinkedCancelOutcome: ...


@runtime_checkable
class ExecutionGateway(Protocol):
    """Protocol for order execution operations."""

    async def place_order(
        self,
        trading_pair: str,
        order_type: OrderType | CanonicalOrderType | str,
        side: TradeType | CanonicalTradeType | str,
        amount: Decimal,
        price: Decimal | None,
    ) -> str:
        pass

    async def cancel_order(self, trading_pair: str, client_order_id: str) -> bool:
        pass

    async def get_open_orders(self, trading_pair: str) -> list[OpenOrder]:
        pass

    async def get_balance(self, currency: str) -> Decimal:
        pass


@runtime_checkable
class MarketDataGateway(Protocol):
    """Protocol for market data operations."""

    async def get_orderbook(self, trading_pair: str) -> OrderBookSnapshot:
        pass

    async def get_candles(self, trading_pair: str, interval: str, limit: int) -> list[CandleData]:
        pass

    async def get_mid_price(self, trading_pair: str) -> Decimal:
        pass

    async def subscribe_orderbook(
        self,
        trading_pair: str,
        callback: Callable[[OrderBookUpdate], None],
    ) -> AsyncContextManager:
        pass

    async def subscribe_trades(
        self,
        trading_pair: str,
        callback: Callable[[TradeEvent], None],
    ) -> AsyncContextManager:
        pass


@runtime_checkable
class ExchangeGateway(ExecutionGateway, MarketDataGateway, Protocol):
    """Composite protocol: execution + market data + lifecycle."""

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    @property
    def ready(self) -> bool:
        return False


@runtime_checkable
class TransportAwareGateway(ExchangeGateway, Protocol):
    """Extension of ExchangeGateway for exchanges that expose typed
    transport slots and (optionally) a structured-contract resolver.

    Existing gateways (Coinbase, Kraken) that do not declare these slots
    continue to satisfy ExchangeGateway unchanged. New gateways
    (Interactive Brokers) implement TransportAwareGateway.
    """

    rest_transport: RequestTransport | None
    stream_transport: StreamTransport | None
    unified_transport: Transport | None
    contract_resolver: ContractResolver | None
