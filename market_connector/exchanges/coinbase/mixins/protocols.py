from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: no cover
    from market_connector.exchanges.coinbase.config import CoinbaseConfig
    from market_connector.transport.endpoint import Endpoint
    from market_connector.transport.rest_base import AuthCallable, RestConnectorBase
    from market_connector.transport.ws_base import WsConnectorBase


class HasRest(Protocol):  # pragma: no cover
    # CoinbaseRestClient is a subclass of RestConnectorBase, so declaring the base
    # is sufficient for structural typing. Mixins only use .request() which is
    # inherited unchanged.
    _rest: RestConnectorBase


class HasWs(Protocol):  # pragma: no cover
    _ws: WsConnectorBase


class HasAuth(Protocol):  # pragma: no cover
    _auth: AuthCallable


class HasEndpoints(Protocol):  # pragma: no cover
    _endpoints: dict[str, Endpoint]


class HasConfig(Protocol):  # pragma: no cover
    _config: CoinbaseConfig


class HasReady(Protocol):  # pragma: no cover
    @property
    def ready(self) -> bool: ...
