"""Kraken :class:`ExchangeDescriptor` (#265); values match the in-tree hummingbot connector."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from market_connector.exchanges.kraken.hb_compat.kraken_connector_core import KrakenConnectorCore
from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway
from market_connector.exchanges.kraken.mixins.orders import KRAKEN_ORDER_CAPABILITIES
from market_connector.exchanges.kraken.schemas.enums import KrakenAPITier
from market_connector.hb_compat.core_protocol import (
    ConfigKey,
    ConnectorCoreHandle,
    ExchangeDescriptor,
    MarketType,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from market_connector.hb_compat.event_bus import EventBus

_DEFAULT_TIER = "Starter"


def _tier(config: Mapping[str, Any]) -> KrakenAPITier:
    raw = config.get("kraken_api_tier") or _DEFAULT_TIER
    try:
        return KrakenAPITier(str(raw).upper())
    except ValueError:
        valid = ", ".join(t.value for t in KrakenAPITier)
        raise ValueError(f"invalid kraken_api_tier {raw!r}; expected one of: {valid}") from None


def _required(config: Mapping[str, Any], key: str) -> str:
    value = config.get(key)
    if not value:
        raise ValueError(f"missing required config key {key!r}")
    return str(value)


async def _create(
    config: Mapping[str, Any], *, event_bus: EventBus, **core_options: Any
) -> ConnectorCoreHandle:
    """Build gateway and core from canonical keys; no network I/O (``handle.start()`` does it)."""
    api_key = _required(config, "kraken_api_key")
    secret_key = _required(config, "kraken_secret_key")
    gateway = KrakenGateway(api_key=api_key, secret_key=secret_key, tier=_tier(config))
    core = KrakenConnectorCore(gateway, event_bus=event_bus, **core_options)
    return ConnectorCoreHandle(
        core=core, gateway=gateway, event_bus=event_bus, descriptor=KRAKEN_DESCRIPTOR
    )


KRAKEN_DESCRIPTOR = ExchangeDescriptor(
    name="kraken",
    display_name="Kraken",
    market_type=MarketType.SPOT,
    config_keys=(
        ConfigKey(
            name="kraken_api_key",
            is_secret=True,
            prompt="Enter your Kraken API key",
            required=True,
            default=None,
        ),
        ConfigKey(
            name="kraken_secret_key",
            is_secret=True,
            prompt="Enter your Kraken secret key",
            required=True,
            default=None,
        ),
        ConfigKey(
            name="kraken_api_tier",
            is_secret=False,
            prompt="Enter your Kraken API Tier (Starter/Intermediate/Pro)",
            required=False,
            default=_DEFAULT_TIER,
        ),
    ),
    example_pair="ETH-USDC",
    default_maker_fee=Decimal("0.0025"),
    default_taker_fee=Decimal("0.004"),
    order_capabilities=KRAKEN_ORDER_CAPABILITIES,
    has_core=True,
    create=_create,
)
