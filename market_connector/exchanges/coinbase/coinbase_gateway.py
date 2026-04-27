"""CoinbaseGateway — composition root for the Coinbase Advanced Trade connector."""

from __future__ import annotations

from typing import TYPE_CHECKING

from market_connector.exchanges.coinbase.factory import coinbase_signer_factory
from market_connector.exchanges.coinbase.endpoints import ENDPOINT_REGISTRY
from market_connector.exchanges.coinbase.mixins.accounts import AccountsMixin
from market_connector.exchanges.coinbase.mixins.market_data import MarketDataMixin
from market_connector.exchanges.coinbase.mixins.orders import OrdersMixin
from market_connector.exchanges.coinbase.mixins.subscriptions import SubscriptionsMixin
from market_connector.exchanges.coinbase.transport import CoinbaseRestClient
from market_connector.transport.ws_base import WsConnectorBase

if TYPE_CHECKING:
    from market_connector.exchanges.coinbase.config import CoinbaseConfig


class CoinbaseGateway(OrdersMixin, AccountsMixin, MarketDataMixin, SubscriptionsMixin):
    """Coinbase Advanced Trade gateway — implements ExchangeGateway protocol."""

    def __init__(self, config: CoinbaseConfig) -> None:
        self._config = config
        self._auth = coinbase_signer_factory(config.api_key, config.secret_key)
        self._endpoints = ENDPOINT_REGISTRY
        self._rest = CoinbaseRestClient(
            base_url=config.base_url,
            endpoints=ENDPOINT_REGISTRY,
            signer=self._auth,
            max_retries=3,
            retry_delay=1.0,
        )
        self._ws = WsConnectorBase(
            url=config.ws_url,
            auth=None,
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
