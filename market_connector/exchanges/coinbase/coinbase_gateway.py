"""CoinbaseGateway — composition root for the Coinbase Advanced Trade connector."""

from __future__ import annotations

from typing import TYPE_CHECKING

from market_connector.exchanges.coinbase.endpoints import ENDPOINT_REGISTRY
from market_connector.exchanges.coinbase.factory import coinbase_signer_factory
from market_connector.exchanges.coinbase.mixins.accounts import AccountsMixin
from market_connector.exchanges.coinbase.mixins.market_data import MarketDataMixin
from market_connector.exchanges.coinbase.mixins.orders import OrdersMixin
from market_connector.exchanges.coinbase.mixins.subscriptions import SubscriptionsMixin
from market_connector.exchanges.coinbase.specs import (
    COINBASE_RATE_LIMIT_SPEC,
    COINBASE_WS_SHAPE_DECODER_SPEC,
)
from market_connector.exchanges.coinbase.transport import CoinbaseRestClient
from market_connector.rate_limits.flat import FlatRateLimit
from market_connector.symbols.mapper import IdentityMapper
from market_connector.transport.ws_base import WsConnectorBase
from market_connector.ws_models.auth_models import PassThroughAuth
from market_connector.ws_models.decoder import JsonEnvelopeDecoder

if TYPE_CHECKING:
    from market_connector.exchanges.coinbase.config import CoinbaseConfig


class CoinbaseGateway(OrdersMixin, AccountsMixin, MarketDataMixin, SubscriptionsMixin):
    """Coinbase Advanced Trade gateway — implements ExchangeGateway protocol."""

    def __init__(self, config: CoinbaseConfig) -> None:
        self._config = config
        self._signer = coinbase_signer_factory(config.api_key, config.secret_key)
        self._mapper = IdentityMapper(separator="-")
        self._rate_limit = FlatRateLimit(spec=COINBASE_RATE_LIMIT_SPEC)
        self._ws_decoder = JsonEnvelopeDecoder(**COINBASE_WS_SHAPE_DECODER_SPEC)
        # PassThroughAuth: Coinbase WS subscribe messages are not transformed via
        # WsConnectorBase.send(); the subscriptions mixin registers handlers directly.
        # PerMessageSignAuth wiring is deferred to a follow-up task once the
        # Coinbase WS JWT-signing spec is formalised.
        self._ws_auth = PassThroughAuth()
        self._endpoints = ENDPOINT_REGISTRY
        self._rest = CoinbaseRestClient(
            base_url=config.base_url,
            endpoints=ENDPOINT_REGISTRY,
            signer=self._signer,
            max_retries=3,
            retry_delay=1.0,
        )
        self._ws = WsConnectorBase(
            url=config.ws_url,
            ws_auth=self._ws_auth,
            decoder=self._ws_decoder,
            heartbeat_interval=30.0,
            reconnect_delay=1.0,
            max_reconnect_delay=60.0,
        )
        self._started = False

    @property
    def ready(self) -> bool:
        return self._started

    async def start(self) -> None:
        """Validate connectivity and open the WebSocket connection."""
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
