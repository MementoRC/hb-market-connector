"""Tests for build_ib_gateway() factory."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_connector.auth.passthrough import PassThroughSigner
from market_connector.exchanges.interactive_brokers.contract_resolver import IbContractResolver
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

    def test_contract_resolver_is_ib_resolver(self):
        # Stage 2 injects IbContractResolver; Stage 1's None is replaced.
        spec = IbConnectionSpec()
        with patch("market_connector.exchanges.interactive_brokers.transport.IB"):
            g = build_ib_gateway(spec)
        assert isinstance(g.contract_resolver, IbContractResolver)

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


class TestFactoryResolverWiring:
    def test_resolver_shares_transport_reference(self):
        """The injected resolver must wrap the same transport instance as the gateway."""
        spec = IbConnectionSpec()
        with patch("market_connector.exchanges.interactive_brokers.transport.IB"):
            g = build_ib_gateway(spec)
        # resolver._transport is the identical object stored in gateway.unified_transport
        assert g.contract_resolver._transport is g.unified_transport

    def test_resolver_accessible_via_gateway_slot(self):
        spec = IbConnectionSpec()
        with patch("market_connector.exchanges.interactive_brokers.transport.IB"):
            g = build_ib_gateway(spec)
        # contract_resolver is a non-None IbContractResolver on the gateway.
        assert g.contract_resolver is not None
        assert isinstance(g.contract_resolver, IbContractResolver)


class TestGatewayDelegation:
    @pytest.mark.asyncio
    async def test_place_order_delegates_to_transport(self):
        """place_order resolves via contract_resolver then delegates to transport."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from market_connector.contracts.instrument import InstrumentRef, InstrumentType
        from market_connector.exchanges.interactive_brokers.factory import build_ib_gateway
        from market_connector.exchanges.interactive_brokers.order_handle import (
            OrderHandle,
        )
        from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec

        spec = IbConnectionSpec()
        with patch("market_connector.exchanges.interactive_brokers.transport.IB"):
            g = build_ib_gateway(spec)

        # Replace resolver and transport with mocks.
        resolved = MagicMock()
        resolved.native = MagicMock()
        g.contract_resolver = MagicMock()
        g.contract_resolver.resolve = AsyncMock(return_value=resolved)

        expected_handle = MagicMock(spec=OrderHandle)
        g.unified_transport.place_order = AsyncMock(return_value=expected_handle)

        ref = InstrumentRef(
            symbol="AAPL", instrument_type=InstrumentType.STOCK, quote_currency="USD"
        )
        hb_order = MagicMock()

        result = await g.place_order(ref, hb_order)

        g.contract_resolver.resolve.assert_awaited_once_with(ref)
        g.unified_transport.place_order.assert_awaited_once_with(resolved.native, hb_order)
        assert result is expected_handle

    @pytest.mark.asyncio
    async def test_place_order_raises_when_no_resolver(self):
        from unittest.mock import patch

        from market_connector.contracts.instrument import InstrumentRef, InstrumentType
        from market_connector.exchanges.interactive_brokers.factory import build_ib_gateway
        from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec

        spec = IbConnectionSpec()
        with patch("market_connector.exchanges.interactive_brokers.transport.IB"):
            g = build_ib_gateway(spec)

        g.contract_resolver = None  # simulate Stage 1 condition
        ref = InstrumentRef(
            symbol="AAPL", instrument_type=InstrumentType.STOCK, quote_currency="USD"
        )

        with pytest.raises(RuntimeError, match="contract_resolver"):
            await g.place_order(ref, MagicMock())

    @pytest.mark.asyncio
    async def test_cancel_order_delegates_to_transport(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        from market_connector.exchanges.interactive_brokers.factory import build_ib_gateway
        from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec

        spec = IbConnectionSpec()
        with patch("market_connector.exchanges.interactive_brokers.transport.IB"):
            g = build_ib_gateway(spec)

        handle = MagicMock()
        cancelled = MagicMock()
        g.unified_transport.cancel_order = AsyncMock(return_value=cancelled)

        result = await g.cancel_order(handle)

        g.unified_transport.cancel_order.assert_awaited_once_with(handle)
        assert result is cancelled

    def test_get_open_orders_delegates_to_transport(self):
        from unittest.mock import MagicMock, patch

        from market_connector.exchanges.interactive_brokers.factory import build_ib_gateway
        from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec

        spec = IbConnectionSpec()
        with patch("market_connector.exchanges.interactive_brokers.transport.IB"):
            g = build_ib_gateway(spec)

        handles = [MagicMock(), MagicMock()]
        g.unified_transport.open_orders = MagicMock(return_value=handles)

        result = g.get_open_orders()

        g.unified_transport.open_orders.assert_called_once()
        assert result is handles
