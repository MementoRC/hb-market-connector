"""Tests for CoinbaseGateway — Phase 7, Task 7.1."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from market_connector.exceptions import GatewayNotStartedError
from market_connector.exchanges.coinbase.coinbase_gateway import CoinbaseGateway
from market_connector.exchanges.coinbase.config import CoinbaseConfig


@pytest.fixture
def cfg() -> CoinbaseConfig:
    return CoinbaseConfig(api_key="k", secret_key="raw_secret_hmac", sandbox=True)


def test_gateway_initial_state_not_ready(cfg: CoinbaseConfig) -> None:
    gw = CoinbaseGateway(cfg)
    assert gw.ready is False


@pytest.mark.asyncio
async def test_pre_start_raises(cfg: CoinbaseConfig) -> None:
    gw = CoinbaseGateway(cfg)
    with pytest.raises(GatewayNotStartedError):
        await gw.get_balance("USD")


@pytest.mark.asyncio
async def test_stop_is_idempotent(cfg: CoinbaseConfig) -> None:
    gw = CoinbaseGateway(cfg)
    await gw.stop()  # not started — must not raise
    await gw.stop()  # second call — still must not raise


@pytest.mark.asyncio
async def test_start_sets_ready(cfg: CoinbaseConfig) -> None:
    gw = CoinbaseGateway(cfg)
    with (
        patch.object(gw._rest, "request", new=AsyncMock(return_value={})),
        patch.object(gw._ws, "connect", new=AsyncMock()),
    ):
        await gw.start()
    assert gw.ready is True


@pytest.mark.asyncio
async def test_start_idempotent(cfg: CoinbaseConfig) -> None:
    """Calling start() twice must not issue two connect() calls."""
    gw = CoinbaseGateway(cfg)
    mock_connect = AsyncMock()
    with (
        patch.object(gw._rest, "request", new=AsyncMock(return_value={})),
        patch.object(gw._ws, "connect", new=mock_connect),
    ):
        await gw.start()
        await gw.start()
    mock_connect.assert_called_once()


@pytest.mark.asyncio
async def test_stop_after_start(cfg: CoinbaseConfig) -> None:
    """stop() after start() must disconnect WS and close REST, then ready=False."""
    gw = CoinbaseGateway(cfg)
    with (
        patch.object(gw._rest, "request", new=AsyncMock(return_value={})),
        patch.object(gw._ws, "connect", new=AsyncMock()),
    ):
        await gw.start()
    assert gw.ready is True

    mock_disconnect = AsyncMock()
    mock_close = AsyncMock()
    with (
        patch.object(gw._ws, "disconnect", new=mock_disconnect),
        patch.object(gw._rest, "close", new=mock_close),
    ):
        await gw.stop()

    assert gw.ready is False
    mock_disconnect.assert_called_once()
    mock_close.assert_called_once()


def test_gateway_composes_all_mixins(cfg: CoinbaseConfig) -> None:
    """Gateway must have all mixin methods available."""
    from market_connector.exchanges.coinbase.mixins.accounts import AccountsMixin
    from market_connector.exchanges.coinbase.mixins.market_data import MarketDataMixin
    from market_connector.exchanges.coinbase.mixins.orders import OrdersMixin
    from market_connector.exchanges.coinbase.mixins.subscriptions import SubscriptionsMixin

    gw = CoinbaseGateway(cfg)
    assert isinstance(gw, AccountsMixin)
    assert isinstance(gw, MarketDataMixin)
    assert isinstance(gw, OrdersMixin)
    assert isinstance(gw, SubscriptionsMixin)


def test_gateway_has_required_protocol_attributes(cfg: CoinbaseConfig) -> None:
    """All Protocol attributes must be present as concrete instances."""
    gw = CoinbaseGateway(cfg)
    assert hasattr(gw, "_config")
    assert hasattr(gw, "_auth")
    assert hasattr(gw, "_rest")
    assert hasattr(gw, "_ws")
    assert hasattr(gw, "_endpoints")
    assert gw._config is cfg
