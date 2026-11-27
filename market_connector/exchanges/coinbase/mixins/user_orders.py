"""UserOrdersMixin: authenticated ``user`` channel subscription (#310 PR 2)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from market_connector.exceptions import GatewayNotStartedError
from market_connector.exchanges.coinbase.user_channel import USER_CHANNEL, UserChannelSession
from market_connector.exchanges.coinbase.ws_auth import CoinbaseJwtWsAuth
from market_connector.transport.ws_base import WsConnectorBase

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Sequence

    from market_connector.exchanges.coinbase.mixins.protocols import HasConfig, HasReady
    from market_connector.exchanges.coinbase.schemas.ws import UserEvent
    from market_connector.ws_models.decoder import WsShapeDecoder


class UserOrdersMixin:
    async def subscribe_user_orders(
        self: HasConfig & HasReady,  # type: ignore[valid-type]
        callback: Callable[[UserEvent], None],
        *,
        product_ids: Sequence[str] | None = None,
        on_snapshot_complete: Callable[[], None] | None = None,
        on_gap: Callable[[int, int], None] | None = None,
    ) -> Any:
        """Return a context manager that streams the account's order events.

        Uses its own :class:`WsConnectorBase` on ``config.ws_user_url`` (the user socket is
        a different host from market data), so reconnect/backoff is the shared machinery.
        """
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")

        config = self._config
        decoder: WsShapeDecoder = self._ws_decoder
        session = UserChannelSession(
            CoinbaseJwtWsAuth(config.api_key, config.secret_key),
            decoder,
            callback,
            product_ids=product_ids,
            on_snapshot_complete=on_snapshot_complete,
            on_gap=on_gap,
        )
        ws = WsConnectorBase(
            url=config.ws_user_url,
            ws_auth=session,
            decoder=session,
            heartbeat_interval=30.0,
            reconnect_delay=1.0,
            max_reconnect_delay=60.0,
        )
        ws.subscribe(USER_CHANNEL, None, session.handle)

        @asynccontextmanager
        async def _ctx() -> AsyncGenerator[None, None]:
            await ws.connect()
            try:
                yield
            finally:
                try:
                    for frame in session.unsubscribe_frames():
                        await ws.send(frame)
                except GatewayNotStartedError:
                    pass  # socket is down mid-reconnect; nothing to unsubscribe from
                finally:
                    await ws.disconnect()

        return _ctx()
