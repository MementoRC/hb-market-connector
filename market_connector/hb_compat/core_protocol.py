"""Exchange descriptor, core protocol and lifecycle handle for the hb_compat layer (#265).

A generic consumer (the hummingbot connector wrapper) discovers exchanges through
:mod:`market_connector.hb_compat.registry`, reads their metadata from an
:class:`ExchangeDescriptor` and builds a started-on-demand :class:`ConnectorCoreHandle`
with :meth:`ExchangeDescriptor.create`. No hummingbot imports (ADR 0001).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, Self, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Mapping
    from decimal import Decimal
    from types import TracebackType

    from data_type_primitives import OrderBookSnapshot, TradingRules
    from data_type_primitives.common import OrderType, PositionAction, PriceType
    from data_type_primitives.in_flight_order import InFlightOrder

    from market_connector.capabilities import OrderCapabilities
    from market_connector.hb_compat.event_bus import EventBus


class MarketType(StrEnum):
    SPOT = "spot"
    PERPETUAL = "perpetual"


@dataclass(frozen=True)
class ConfigKey:
    """One user-facing configuration entry of an exchange."""

    name: str
    is_secret: bool
    prompt: str
    required: bool
    default: str | None


@runtime_checkable
class ConnectorCoreProtocol(Protocol):
    """Order, market-data and balance core of one exchange (see ``KrakenConnectorCore``)."""

    @property
    def trading_rules(self) -> Mapping[str, TradingRules]: ...

    @property
    def in_flight_orders(self) -> Mapping[str, InFlightOrder]: ...

    @property
    def status_dict(self) -> dict[str, bool]: ...

    @property
    def ready(self) -> bool: ...

    def buy(
        self,
        trading_pair: str,
        amount: Decimal,
        order_type: OrderType = ...,
        price: Decimal = ...,
        *,
        limit_price: Decimal | None = ...,
        price_in_percent: bool = ...,
        position_action: PositionAction | None = ...,
        expiration_ts: float | None = ...,
    ) -> str: ...

    def sell(
        self,
        trading_pair: str,
        amount: Decimal,
        order_type: OrderType = ...,
        price: Decimal = ...,
        *,
        limit_price: Decimal | None = ...,
        price_in_percent: bool = ...,
        position_action: PositionAction | None = ...,
        expiration_ts: float | None = ...,
    ) -> str: ...

    def cancel(self, trading_pair: str, client_order_id: str) -> str: ...

    def fetch_order(self, client_order_id: str) -> InFlightOrder | None: ...

    def client_order_id_for_txid(self, txid: str) -> str | None: ...

    def txid_for_client_order_id(self, client_order_id: str) -> str | None: ...

    def get_price_by_type(self, trading_pair: str, price_type: PriceType) -> Decimal: ...

    def get_order_book(self, trading_pair: str) -> OrderBookSnapshot | None: ...

    def quantize_order_amount(self, trading_pair: str, amount: Decimal) -> Decimal: ...

    def quantize_order_price(self, trading_pair: str, price: Decimal) -> Decimal: ...

    def get_order_price_quantum(self, trading_pair: str, price: Decimal) -> Decimal: ...

    def get_order_size_quantum(self, trading_pair: str, amount: Decimal) -> Decimal: ...

    def get_balance(self, asset: str) -> Decimal: ...

    def get_available_balance(self, asset: str) -> Decimal: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def update_order_status(self, client_order_id: str) -> None: ...

    async def update_prices(self, trading_pairs: Iterable[str]) -> None: ...

    async def update_order_book(self, trading_pair: str) -> None: ...

    async def update_trading_rules(self) -> None: ...

    async def update_balances(self) -> None: ...


class _Lifecycle(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class ConnectorCoreHandle:
    """A created (not yet started) gateway + core pair.

    ``start()`` starts the gateway, then the core; ``close()`` stops the core, then the
    gateway, and is idempotent. A closed handle cannot be restarted.
    """

    def __init__(
        self,
        *,
        core: ConnectorCoreProtocol,
        gateway: Any,
        event_bus: EventBus,
        descriptor: ExchangeDescriptor,
    ) -> None:
        self.core = core
        self.gateway: _Lifecycle = gateway
        self.event_bus = event_bus
        self.descriptor = descriptor
        self._closed = False

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("ConnectorCoreHandle is closed and cannot be restarted")
        await self.gateway.start()
        await self.core.start()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self.core.stop()
        finally:
            await self.gateway.stop()

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()


@dataclass(frozen=True)
class ExchangeDescriptor:
    """Static metadata of an exchange plus the factory for its core handle."""

    name: str
    display_name: str
    market_type: MarketType
    config_keys: tuple[ConfigKey, ...]
    example_pair: str
    default_maker_fee: Decimal
    default_taker_fee: Decimal
    order_capabilities: OrderCapabilities
    has_core: bool
    create: Callable[..., Awaitable[ConnectorCoreHandle]]

    def config_keys_with_prefix(self, prefix: str) -> tuple[ConfigKey, ...]:
        """Keys renamed ``<prefix>_<suffix>`` (``kraken_api_key`` -> ``kraken_mc_api_key``)."""
        head = f"{self.name}_"
        return tuple(
            dataclasses.replace(
                key,
                name=f"{prefix}_{key.name.removeprefix(head)}",
            )
            for key in self.config_keys
        )
