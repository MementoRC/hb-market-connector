"""Tests for IbGatewayTransport (Stage 1: connect/disconnect only).

These tests mock ib_async.IB to avoid requiring a real IB Gateway.
The integration test that hits a real Docker-hosted Gateway lives in
test_integration.py and is gated by pytest.mark.ib_gateway.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.transport import IbGatewayTransport
from market_connector.transport.protocols import (
    RequestTransport,
    StreamTransport,
    Transport,
)


@pytest.fixture
def spec():
    return IbConnectionSpec(host="127.0.0.1", port=4002, client_id=1, paper=True)


@pytest.fixture
def mock_ib():
    """Mock ib_async.IB instance."""
    ib = MagicMock()
    ib.connectAsync = AsyncMock()
    ib.disconnect = MagicMock()
    ib.isConnected = MagicMock(return_value=True)
    return ib


class TestProtocolConformance:
    def test_satisfies_request_transport(self, spec):
        with patch("market_connector.exchanges.interactive_brokers.transport.IB"):
            t = IbGatewayTransport(spec)
            assert isinstance(t, RequestTransport)

    def test_satisfies_stream_transport(self, spec):
        with patch("market_connector.exchanges.interactive_brokers.transport.IB"):
            t = IbGatewayTransport(spec)
            assert isinstance(t, StreamTransport)

    def test_satisfies_unified_transport(self, spec):
        # Transport is the intersection: requires both request() and subscribe().
        with patch("market_connector.exchanges.interactive_brokers.transport.IB"):
            t = IbGatewayTransport(spec)
            assert isinstance(t, Transport)


class TestConnectionLifecycle:
    def test_init_raises_import_error_if_ib_async_missing(self, spec):
        """When ib_async is not installed, IB is None and __init__ raises ImportError."""
        with (
            patch(
                "market_connector.exchanges.interactive_brokers.transport.IB",
                None,
            ),
            pytest.raises(ImportError, match="ib_async"),
        ):
            IbGatewayTransport(spec)

    @pytest.mark.asyncio
    async def test_connect_calls_ib_connect_async(self, spec, mock_ib):
        with patch(
            "market_connector.exchanges.interactive_brokers.transport.IB",
            return_value=mock_ib,
        ):
            t = IbGatewayTransport(spec)
            await t.connect()
            mock_ib.connectAsync.assert_awaited_once_with(host="127.0.0.1", port=4002, clientId=1)

    @pytest.mark.asyncio
    async def test_is_connected_reflects_ib_state(self, spec, mock_ib):
        with patch(
            "market_connector.exchanges.interactive_brokers.transport.IB",
            return_value=mock_ib,
        ):
            t = IbGatewayTransport(spec)
            await t.connect()
            assert t.is_connected is True
            mock_ib.isConnected.return_value = False
            assert t.is_connected is False

    @pytest.mark.asyncio
    async def test_disconnect_calls_ib_disconnect(self, spec, mock_ib):
        with patch(
            "market_connector.exchanges.interactive_brokers.transport.IB",
            return_value=mock_ib,
        ):
            t = IbGatewayTransport(spec)
            await t.connect()
            await t.disconnect()
            mock_ib.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_request_not_implemented_in_stage1(self, spec, mock_ib):
        with patch(
            "market_connector.exchanges.interactive_brokers.transport.IB",
            return_value=mock_ib,
        ):
            t = IbGatewayTransport(spec)
            with pytest.raises(NotImplementedError, match="Stage 2"):
                await t.request("reqContractDetails")

    def test_subscribe_not_implemented_in_stage1(self, spec, mock_ib):
        with patch(
            "market_connector.exchanges.interactive_brokers.transport.IB",
            return_value=mock_ib,
        ):
            t = IbGatewayTransport(spec)
            with pytest.raises(NotImplementedError, match="Stage 3"):
                t.subscribe("ticker", "AAPL", lambda x: None)
