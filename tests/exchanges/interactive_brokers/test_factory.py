"""Tests for build_ib_gateway() factory."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_connector.auth.passthrough import PassThroughSigner
from market_connector.exchanges.interactive_brokers.factory import build_ib_gateway
from market_connector.exchanges.interactive_brokers.ib_gateway import IbGatewayGateway
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.protocols import TransportAwareGateway


@pytest.fixture
def mock_ib():
    """Mock ib_async.IB instance with AsyncMock for connectAsync."""
    ib = MagicMock()
    ib.connectAsync = AsyncMock()
    ib.disconnect = MagicMock()
    ib.isConnected = MagicMock(return_value=False)
    return ib


class TestBuildIbGateway:
    def test_returns_gateway_instance(self):
        spec = IbConnectionSpec()
        with patch("market_connector.exchanges.interactive_brokers.transport.IB"):
            g = build_ib_gateway(spec)
        assert isinstance(g, IbGatewayGateway)

    def test_satisfies_transport_aware_protocol(self):
        spec = IbConnectionSpec()
        with patch("market_connector.exchanges.interactive_brokers.transport.IB"):
            g = build_ib_gateway(spec)
        assert isinstance(g, TransportAwareGateway)

    def test_unified_transport_set_rest_and_stream_none(self):
        spec = IbConnectionSpec()
        with patch("market_connector.exchanges.interactive_brokers.transport.IB"):
            g = build_ib_gateway(spec)
        assert g.rest_transport is None
        assert g.stream_transport is None
        assert g.unified_transport is not None

    def test_signer_is_passthrough(self):
        spec = IbConnectionSpec()
        with patch("market_connector.exchanges.interactive_brokers.transport.IB"):
            g = build_ib_gateway(spec)
        assert isinstance(g.signer, PassThroughSigner)

    def test_contract_resolver_none_in_stage1(self):
        # Stage 1 has no resolver yet; Stage 2 adds IbContractResolver.
        spec = IbConnectionSpec()
        with patch("market_connector.exchanges.interactive_brokers.transport.IB"):
            g = build_ib_gateway(spec)
        assert g.contract_resolver is None

    @pytest.mark.asyncio
    async def test_lifecycle(self, mock_ib):
        spec = IbConnectionSpec()
        with patch(
            "market_connector.exchanges.interactive_brokers.transport.IB",
            return_value=mock_ib,
        ):
            g = build_ib_gateway(spec)
            assert g.ready is False

            await g.start()
            mock_ib.connectAsync.assert_awaited_once()

            mock_ib.isConnected.return_value = True
            assert g.ready is True

            await g.stop()
            mock_ib.disconnect.assert_called_once()
