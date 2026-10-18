"""Tests for IbGatewayTransport — connect/disconnect lifecycle and Stage 2 wiring.

These tests mock ib_async.IB to avoid requiring a real IB Gateway.
The integration test that hits a real Docker-hosted Gateway lives in
test_integration.py and is gated by pytest.mark.ib_gateway.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_connector.exchanges.interactive_brokers._error_router import _ErrorRouter
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.transport import DepthBook, IbGatewayTransport
from market_connector.transport.protocols import (
    RequestTransport,
    StreamTransport,
    Transport,
)

from .test_persistent_market_data import _feed, _tick


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


class TestMarketDataType:
    """Issue #53: connect() sends reqMarketDataType with the resolved value."""

    @pytest.mark.parametrize(
        "spec,expected",
        [
            (IbConnectionSpec(port=4002, paper=True), 3),
            (IbConnectionSpec(port=4001, paper=False), 1),
            (IbConnectionSpec(port=4002, paper=True, market_data_type=1), 1),
            (IbConnectionSpec(port=4001, paper=False, market_data_type=4), 4),
        ],
    )
    async def test_connect_requests_resolved_market_data_type(
        self, spec: IbConnectionSpec, expected: int
    ) -> None:
        from tests.exchanges.interactive_brokers._doubles import MockIb

        ib = MockIb()
        with patch("market_connector.exchanges.interactive_brokers.transport.IB", return_value=ib):
            transport = IbGatewayTransport(spec)
            await transport.connect()
            assert ib.market_data_type_calls == [expected]
            transport._apply_market_data_type()
            assert ib.market_data_type_calls == [expected, expected]
        assert transport._error_router.market_data_type == expected


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
            mock_ib.connectAsync.assert_awaited_once_with(
                host="127.0.0.1",
                port=4002,
                clientId=1,
                readonly=False,
                account="",
                raiseSyncErrors=True,
            )

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


class TestRegistries:
    def test_handle_registry_initially_empty(self, spec: IbConnectionSpec) -> None:
        with patch("market_connector.exchanges.interactive_brokers.transport.IB"):
            t = IbGatewayTransport(spec)
            assert t._handle_registry == {}

    def test_error_router_created_on_init(self, spec: IbConnectionSpec) -> None:
        with patch("market_connector.exchanges.interactive_brokers.transport.IB"):
            t = IbGatewayTransport(spec)
            assert isinstance(t._error_router, _ErrorRouter)


class TestSubscribe:
    def test_subscribe_depth_returns_handle(self, spec: IbConnectionSpec) -> None:
        from ._doubles import MockIb

        mock_ib = MockIb()
        with patch(
            "market_connector.exchanges.interactive_brokers.transport.IB",
            return_value=mock_ib,
        ):
            transport = IbGatewayTransport(spec)
        contract = MagicMock()
        transport._ib._depth_ticker.domBids = []
        transport._ib._depth_ticker.domAsks = []

        handle = transport.subscribe("depth", contract, lambda ticker: None)

        assert handle is not None
        assert hasattr(handle, "close")

    def test_subscribe_depth_registers_update_event(self, spec: IbConnectionSpec) -> None:
        from ._doubles import MockIb

        mock_ib = MockIb()
        with patch(
            "market_connector.exchanges.interactive_brokers.transport.IB",
            return_value=mock_ib,
        ):
            transport = IbGatewayTransport(spec)
        contract = MagicMock()
        received: list[object] = []

        def on_update(book: object) -> None:
            received.append(book)

        handle = transport.subscribe("depth", contract, on_update)

        # Simulate IB delivering one raw depth update (insert a bid at position 0), then the event
        _feed(transport._ib._depth_ticker, _tick(1, 0, 0, 100.0, 5.0))
        assert len(received) == 1
        assert isinstance(received[0], DepthBook)
        assert list(received[0].bids) == [(Decimal("100.0"), Decimal("5.0"))]

        handle.close()
        # After close, another update should NOT call the callback
        _feed(transport._ib._depth_ticker, _tick(1, 0, 1, 99.0, 6.0))
        assert len(received) == 1  # still 1

    def test_close_is_idempotent(self, spec: IbConnectionSpec) -> None:
        from ._doubles import MockIb

        mock_ib = MockIb()
        with patch(
            "market_connector.exchanges.interactive_brokers.transport.IB",
            return_value=mock_ib,
        ):
            transport = IbGatewayTransport(spec)
        contract = MagicMock()
        handle = transport.subscribe("depth", contract, lambda t: None)
        handle.close()
        handle.close()  # must not raise

    async def test_close_calls_cancel_mkt_depth_after_idle_release(self) -> None:
        """Issue #54 (D4): the last handle closing cancels after depth_idle_release, not at once."""
        import asyncio

        from ._doubles import MockIb

        mock_ib = MockIb()
        with patch(
            "market_connector.exchanges.interactive_brokers.transport.IB",
            return_value=mock_ib,
        ):
            transport = IbGatewayTransport(IbConnectionSpec(depth_idle_release=0.05))
        contract = MagicMock()
        handle = transport.subscribe("depth", contract, lambda t: None)
        handle.close()
        assert transport._ib._cancel_mkt_depth_calls == []
        await asyncio.sleep(0.3)
        assert len(transport._ib._cancel_mkt_depth_calls) == 1

    def test_subscribe_trades_returns_handle(self, spec: IbConnectionSpec) -> None:
        from ._doubles import MockIb

        mock_ib = MockIb()
        with patch(
            "market_connector.exchanges.interactive_brokers.transport.IB",
            return_value=mock_ib,
        ):
            transport = IbGatewayTransport(spec)
        contract = MagicMock()

        handle = transport.subscribe("trades", contract, lambda tick: None)
        assert handle is not None
        handle.close()
        assert transport._ib._cancel_tick_by_tick_calls == [(contract, "AllLast")]

    def _trades_transport(self, spec: IbConnectionSpec) -> IbGatewayTransport:
        from ._doubles import MockIb

        with patch(
            "market_connector.exchanges.interactive_brokers.transport.IB",
            return_value=MockIb(),
        ):
            return IbGatewayTransport(spec)

    def test_trades_publish_one_tick_per_tick_by_tick_entry(self, spec: IbConnectionSpec) -> None:
        transport = self._trades_transport(spec)
        received: list[object] = []
        transport.subscribe("trades", MagicMock(), received.append)

        ticker = transport._ib._tick_ticker
        ticks = [MagicMock(), MagicMock(), MagicMock()]
        ticker.tickByTicks.extend(ticks)
        ticker.updateEvent(ticker)

        assert received == ticks

    def test_trades_empty_tick_by_ticks_publishes_nothing(self, spec: IbConnectionSpec) -> None:
        transport = self._trades_transport(spec)
        received: list[object] = []
        transport.subscribe("trades", MagicMock(), received.append)

        ticker = transport._ib._tick_ticker
        ticker.updateEvent(ticker)

        assert received == []

    def test_trades_close_detaches_update_handler(self, spec: IbConnectionSpec) -> None:
        transport = self._trades_transport(spec)
        received: list[object] = []
        handle = transport.subscribe("trades", MagicMock(), received.append)

        ticker = transport._ib._tick_ticker
        assert len(ticker.updateEvent._handlers) == 1
        handle.close()
        assert ticker.updateEvent._handlers == []

        ticker.tickByTicks.append(MagicMock())
        ticker.updateEvent(ticker)
        assert received == []
