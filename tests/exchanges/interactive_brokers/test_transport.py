"""Tests for IbGatewayTransport — connect/disconnect lifecycle and Stage 2 wiring.

These tests mock ib_async.IB to avoid requiring a real IB Gateway.
The integration test that hits a real Docker-hosted Gateway lives in
test_integration.py and is gated by pytest.mark.ib_gateway.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_connector.exchanges.interactive_brokers._error_router import _ErrorRouter
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.transport import IbGatewayTransport
from market_connector.transport.protocols import (
    RequestTransport,
    StreamTransport,
    Transport,
)


@pytest.fixture
def spec() -> IbConnectionSpec:
    return IbConnectionSpec(host="127.0.0.1", port=4002, client_id=1, paper=True)


@pytest.fixture
def mock_ib() -> MagicMock:
    """Mock ib_async.IB instance."""
    ib = MagicMock()
    ib.connectAsync = AsyncMock()
    ib.disconnect = MagicMock()
    ib.isConnected = MagicMock(return_value=True)
    return ib


class TestProtocolConformance:
    def test_satisfies_request_transport(self, spec: IbConnectionSpec) -> None:
        with patch("market_connector.exchanges.interactive_brokers.transport.IB"):
            t = IbGatewayTransport(spec)
            assert isinstance(t, RequestTransport)

    def test_satisfies_stream_transport(self, spec: IbConnectionSpec) -> None:
        with patch("market_connector.exchanges.interactive_brokers.transport.IB"):
            t = IbGatewayTransport(spec)
            assert isinstance(t, StreamTransport)

    def test_satisfies_unified_transport(self, spec: IbConnectionSpec) -> None:
        # Transport is the intersection: requires both request() and subscribe().
        with patch("market_connector.exchanges.interactive_brokers.transport.IB"):
            t = IbGatewayTransport(spec)
            assert isinstance(t, Transport)


class TestConnectionLifecycle:
    def test_init_raises_import_error_if_ib_async_missing(self, spec: IbConnectionSpec) -> None:
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
    async def test_connect_calls_ib_connect_async(
        self, spec: IbConnectionSpec, mock_ib: MagicMock
    ) -> None:
        with patch(
            "market_connector.exchanges.interactive_brokers.transport.IB",
            return_value=mock_ib,
        ):
            t = IbGatewayTransport(spec)
            await t.connect()
            mock_ib.connectAsync.assert_awaited_once_with(host="127.0.0.1", port=4002, clientId=1)

    @pytest.mark.asyncio
    async def test_connect_wires_error_router(
        self, spec: IbConnectionSpec, mock_ib: MagicMock
    ) -> None:
        # MagicMock records += as call.errorEvent.__iadd__(callback).
        # Bound method identity varies per access; compare via == (method equality).
        with patch(
            "market_connector.exchanges.interactive_brokers.transport.IB",
            return_value=mock_ib,
        ):
            t = IbGatewayTransport(spec)
            await t.connect()
            iadd_calls = [c for c in mock_ib.mock_calls if c[0] == "errorEvent.__iadd__"]
            assert len(iadd_calls) == 1
            assert iadd_calls[0].args[0] == t._error_router.on_error

    @pytest.mark.asyncio
    async def test_disconnect_unwires_error_router(
        self, spec: IbConnectionSpec, mock_ib: MagicMock
    ) -> None:
        # MagicMock records -= as call.errorEvent.__iadd__().__isub__(callback)
        # because += returns a new MagicMock and -= is called on that return value.
        with patch(
            "market_connector.exchanges.interactive_brokers.transport.IB",
            return_value=mock_ib,
        ):
            t = IbGatewayTransport(spec)
            await t.connect()
            await t.disconnect()
            isub_calls = [c for c in mock_ib.mock_calls if c[0] == "errorEvent.__iadd__().__isub__"]
            assert len(isub_calls) == 1
            assert isub_calls[0].args[0] == t._error_router.on_error

    @pytest.mark.asyncio
    async def test_is_connected_reflects_ib_state(
        self, spec: IbConnectionSpec, mock_ib: MagicMock
    ) -> None:
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
    async def test_disconnect_calls_ib_disconnect(
        self, spec: IbConnectionSpec, mock_ib: MagicMock
    ) -> None:
        with patch(
            "market_connector.exchanges.interactive_brokers.transport.IB",
            return_value=mock_ib,
        ):
            t = IbGatewayTransport(spec)
            await t.connect()
            await t.disconnect()
            mock_ib.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_request_not_implemented_use_typed_methods(
        self, spec: IbConnectionSpec, mock_ib: MagicMock
    ) -> None:
        with patch(
            "market_connector.exchanges.interactive_brokers.transport.IB",
            return_value=mock_ib,
        ):
            t = IbGatewayTransport(spec)
            with pytest.raises(
                NotImplementedError,
                match="use typed methods on IbGatewayTransport directly",
            ):
                await t.request("reqContractDetails")

    def test_subscribe_not_implemented_in_stage1(
        self, spec: IbConnectionSpec, mock_ib: MagicMock
    ) -> None:
        with patch(
            "market_connector.exchanges.interactive_brokers.transport.IB",
            return_value=mock_ib,
        ):
            t = IbGatewayTransport(spec)
            with pytest.raises(NotImplementedError, match="Stage 3"):
                t.subscribe("ticker", "AAPL", lambda x: None)


class TestRegistries:
    def test_handle_registry_initially_empty(self, spec: IbConnectionSpec) -> None:
        with patch("market_connector.exchanges.interactive_brokers.transport.IB"):
            t = IbGatewayTransport(spec)
            assert t._handle_registry == {}

    def test_error_router_created_on_init(self, spec: IbConnectionSpec) -> None:
        with patch("market_connector.exchanges.interactive_brokers.transport.IB"):
            t = IbGatewayTransport(spec)
            assert isinstance(t._error_router, _ErrorRouter)
