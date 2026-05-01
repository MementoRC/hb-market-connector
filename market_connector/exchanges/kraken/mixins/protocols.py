from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: no cover
    from market_connector.exchanges.kraken.config import KrakenConfig
    from market_connector.transport.endpoint import Endpoint
    from market_connector.transport.rest_base import RestConnectorBase
    from market_connector.transport.ws_base import WsConnectorBase


class HasRest(Protocol):  # pragma: no cover
    _rest: RestConnectorBase


class HasWs(Protocol):  # pragma: no cover
    _ws: WsConnectorBase


class HasEndpoints(Protocol):  # pragma: no cover
    _endpoints: dict[str, Endpoint]


class HasConfig(Protocol):  # pragma: no cover
    _config: KrakenConfig


class HasReady(Protocol):  # pragma: no cover
    @property
    def ready(self) -> bool:
        return False
