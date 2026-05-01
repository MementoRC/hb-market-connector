"""KrakenGateway — composition root for the Kraken connector."""

from __future__ import annotations

from dataclasses import dataclass, field

from market_connector.exchanges.kraken.endpoints import ENDPOINT_REGISTRY
from market_connector.exchanges.kraken.factory import (
    kraken_signer_factory,
    kraken_ws_auth_factory,
)
from market_connector.exchanges.kraken.mixins.accounts import AccountsMixin
from market_connector.exchanges.kraken.mixins.market_data import MarketDataMixin
from market_connector.exchanges.kraken.mixins.orders import OrdersMixin
from market_connector.exchanges.kraken.mixins.subscriptions import SubscriptionsMixin
from market_connector.exchanges.kraken.schemas.enums import KrakenAPITier
from market_connector.exchanges.kraken.specs import (
    KRAKEN_RATE_LIMIT_SPEC,
    KRAKEN_SYMBOL_MAPPER,
    KRAKEN_WS_DECODER,
)
from market_connector.exchanges.kraken.transport import KrakenTransport
from market_connector.rate_limits.tiered import TieredRateLimit
from market_connector.transport.ws_base import WsConnectorBase

# ---------------------------------------------------------------------------
# URL constants
# ---------------------------------------------------------------------------

_KRAKEN_REST_URL = "https://api.kraken.com"
_KRAKEN_SANDBOX_REST_URL = "https://demo-futures.kraken.com"
_KRAKEN_WS_URL = "wss://ws.kraken.com"
_KRAKEN_WS_AUTH_URL = "wss://ws-auth.kraken.com"


# ---------------------------------------------------------------------------
# Thin config container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KrakenConfig:
    """Runtime configuration for :class:`KrakenGateway`.

    Attributes:
        api_key:    Kraken API key (public component).
        secret_key: Kraken API secret (base64-encoded private key).
        tier:       Rate-limit tier; defaults to ``STARTER``.
        sandbox:    When ``True``, use the Kraken Futures sandbox base URL.
        base_url:   Override the REST base URL (e.g. for testing).
        ws_url:     Override the public WS URL.
        ws_auth_url: Override the private WS URL.
    """

    api_key: str
    secret_key: str
    tier: KrakenAPITier = KrakenAPITier.STARTER
    sandbox: bool = False
    base_url: str = field(default="")
    ws_url: str = field(default="")
    ws_auth_url: str = field(default="")

    def __post_init__(self) -> None:
        # Fill in URL defaults after frozen dataclass construction via object.__setattr__
        if not self.base_url:
            object.__setattr__(
                self,
                "base_url",
                _KRAKEN_SANDBOX_REST_URL if self.sandbox else _KRAKEN_REST_URL,
            )
        if not self.ws_url:
            object.__setattr__(self, "ws_url", _KRAKEN_WS_URL)
        if not self.ws_auth_url:
            object.__setattr__(self, "ws_auth_url", _KRAKEN_WS_AUTH_URL)


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------


class KrakenGateway(OrdersMixin, AccountsMixin, MarketDataMixin, SubscriptionsMixin):
    """Kraken gateway — implements ExchangeGateway protocol via mixin composition.

    MRO is: OrdersMixin → AccountsMixin → MarketDataMixin → SubscriptionsMixin.
    This mirrors the Coinbase pattern where Orders comes first (trading priority).

    Args:
        api_key:    Kraken API key.
        secret_key: Kraken API secret.
        tier:       Rate-limit tier profile (default ``STARTER``).
        sandbox:    Use sandbox REST URL when ``True``.
    """

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        tier: KrakenAPITier = KrakenAPITier.STARTER,
        sandbox: bool = False,
    ) -> None:
        self._config = KrakenConfig(
            api_key=api_key,
            secret_key=secret_key,
            tier=tier,
            sandbox=sandbox,
        )
        self._signer = kraken_signer_factory(api_key, secret_key)
        self._mapper = KRAKEN_SYMBOL_MAPPER
        self._rate_limit = TieredRateLimit(spec=KRAKEN_RATE_LIMIT_SPEC, active_tier=str(tier))
        self._ws_decoder = KRAKEN_WS_DECODER
        self._endpoints = ENDPOINT_REGISTRY

        self._rest = KrakenTransport(
            base_url=self._config.base_url,
            endpoints=ENDPOINT_REGISTRY,
            signer=self._signer,
            max_retries=3,
            retry_delay=1.0,
        )

        # Public WebSocket (no auth); private WS auth bound after rest client is ready.
        _public_ws_auth, _private_ws_auth = kraken_ws_auth_factory(rest_client=self._rest)
        self._ws_auth = _public_ws_auth
        self._ws_private_auth = _private_ws_auth

        self._ws = WsConnectorBase(
            url=self._config.ws_url,
            ws_auth=self._ws_auth,
            decoder=self._ws_decoder,
            heartbeat_interval=30.0,
            reconnect_delay=1.0,
            max_reconnect_delay=60.0,
        )
        self._started = False

    @property
    def ready(self) -> bool:
        """``True`` after :meth:`start` has completed successfully."""
        return self._started

    async def start(self) -> None:
        """Validate connectivity and open the public WebSocket connection."""
        if self._started:
            return
        await self._rest.request("server_time")
        await self._ws.connect()
        self._started = True

    async def stop(self) -> None:
        """Gracefully close WebSocket and REST connections."""
        if not self._started:
            return
        await self._ws.disconnect()
        await self._rest.close()
        self._started = False


__all__ = [
    "KrakenConfig",
    "KrakenGateway",
]
