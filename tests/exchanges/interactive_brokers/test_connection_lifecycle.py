"""Tests for the IB connection lifecycle (issue #51).

Covers strict connect, +PACEAPI, 1101 resubscribe, the reconnect supervisor,
the connection-state bus topic and the new IbConnectionSpec fields. All run
against MockIb; the real-Gateway integration tests live in test_integration.py.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from market_connector.exchanges.interactive_brokers._error_router import _ErrorRouter
from market_connector.exchanges.interactive_brokers.exceptions import ConnectionLostError
from market_connector.exchanges.interactive_brokers.ib_gateway import IbGatewayGateway
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.transport import (
    CONNECTION_TOPIC,
    ConnectionStateEvent,
    DepthBook,
    IbGatewayTransport,
    IbServerInfo,
)

from ._doubles import MockIb
from .test_persistent_market_data import _feed, _tick

_IB_PATH = "market_connector.exchanges.interactive_brokers.transport.IB"


def _make(spec: IbConnectionSpec | None = None) -> tuple[IbGatewayTransport, MockIb, list[float]]:
    """Build a transport over MockIb with an instant, recording sleep."""
    ib = MockIb()
    with patch(_IB_PATH, return_value=ib):
        transport = IbGatewayTransport(spec or IbConnectionSpec())
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        await asyncio.sleep(0)

    transport._sleep = fake_sleep
    return transport, ib, delays


def _record_states(transport: IbGatewayTransport) -> list[str]:
    states: list[str] = []
    transport.subscribe_connection_state(lambda event: states.append(event.state))
    return states


class TestSpecValidation:
    def test_defaults(self) -> None:
        spec = IbConnectionSpec()
        assert spec.readonly is False
        assert spec.pace_api is True
        assert spec.reconnect_initial_delay == 1.0
        assert spec.reconnect_max_delay == 60.0

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"reconnect_initial_delay": 0},
            {"reconnect_initial_delay": -1.0},
            {"reconnect_max_delay": 0},
            {"reconnect_initial_delay": 5.0, "reconnect_max_delay": 1.0},
        ],
    )
    def test_invalid_delays_rejected(self, kwargs: dict[str, float]) -> None:
        with pytest.raises(ValueError, match="reconnect"):
            IbConnectionSpec(**kwargs)

    def test_equal_delays_allowed(self) -> None:
        IbConnectionSpec(reconnect_initial_delay=2.0, reconnect_max_delay=2.0)


class TestStrictConnect:
    async def test_connect_kwargs_and_paceapi(self) -> None:
        transport, ib, _ = _make(IbConnectionSpec(readonly=True, account_id="DU123", client_id=7))
        await transport.connect()
        assert ib.client.connect_options == ["+PACEAPI"]
        assert ib.call_order == ["setConnectOptions", "connectAsync"]
        assert ib.connect_calls == [
            {
                "host": "127.0.0.1",
                "port": 4002,
                "clientId": 7,
                "readonly": True,
                "account": "DU123",
                "timeout": 15.0,
                "raiseSyncErrors": True,
            }
        ]
        assert transport._error_router.is_connected is True

    async def test_paceapi_disabled(self) -> None:
        transport, ib, _ = _make(IbConnectionSpec(pace_api=False))
        await transport.connect()
        assert ib.client.connect_options == []

    async def test_failed_first_connect_propagates_and_attaches_nothing(self) -> None:
        transport, ib, _ = _make()
        states = _record_states(transport)
        ib.connect_failures = 1
        with pytest.raises(ConnectionRefusedError):
            await transport.connect()
        assert ib.errorEvent._handlers == []
        assert ib.disconnectedEvent._handlers == []
        assert transport._error_router.is_connected is False
        assert ib.market_data_type_calls == []
        assert states == []

    async def test_handlers_attached_once_and_market_data_type_applied(self) -> None:
        transport, ib, _ = _make()
        await transport.connect()
        assert len(ib.errorEvent._handlers) == 1
        assert len(ib.disconnectedEvent._handlers) == 1
        assert ib.market_data_type_calls == [3]

    async def test_handler_attach_failure_detaches_and_raises(self) -> None:
        transport, ib, _ = _make()
        with (
            patch.object(transport, "_apply_market_data_type", side_effect=RuntimeError("boom")),
            pytest.raises(RuntimeError, match="boom"),
        ):
            await transport.connect()
        assert ib.errorEvent._handlers == []
        assert ib.disconnectedEvent._handlers == []


class TestErrorRouterCodes:
    def test_1101_marks_connected_and_notifies_both_listener_kinds(self) -> None:
        router = _ErrorRouter()
        ups: list[bool] = []
        lost: list[tuple[int, str]] = []
        router._connection_listeners.append(ups.append)
        router._data_lost_listeners.append(lambda code, msg: lost.append((code, msg)))
        router.on_error(req_id=-1, code=1101, msg="data lost")
        assert router.is_connected is True
        assert ups == [True]
        assert lost == [(1101, "data lost")]

    def test_1102_does_not_call_data_lost_listeners(self) -> None:
        router = _ErrorRouter()
        lost: list[tuple[int, str]] = []
        router._data_lost_listeners.append(lambda code, msg: lost.append((code, msg)))
        router.on_error(req_id=-1, code=1102, msg="restored")
        assert lost == []
        assert router.is_connected is True


class TestResubscribeOn1101:
    async def test_1101_resubscribes_depth_and_trades_on_new_tickers(self) -> None:
        transport, ib, _ = _make()
        states = _record_states(transport)
        await transport.connect()
        depth: list[object] = []
        trades: list[object] = []
        transport.subscribe("depth", "C", depth.append)
        transport.subscribe("trades", "C", trades.append)
        old_depth, old_ticks = ib._depth_ticker, ib._tick_ticker
        ib.rotate_tickers()

        ib.errorEvent(-1, 1101, "data lost")

        # Old streams cancelled first, then re-requested.
        assert ib._cancel_mkt_depth_calls == ["C"]
        assert ib._cancel_tick_by_tick_calls == [("C", "AllLast")]
        assert len(ib.req_mkt_depth_calls) == 2
        assert len(ib.req_tick_by_tick_calls) == 2
        assert old_depth.updateEvent._handlers == []
        assert old_ticks.updateEvent._handlers == []
        assert states[-2:] == ["connected", "data_lost"]

        # Callbacks keep receiving, now from the NEW tickers.
        _feed(ib._depth_ticker, _tick(1, 0, 0, 100.0, 5.0))
        tick = MagicMock()
        ib._tick_ticker.tickByTicks.append(tick)
        ib._tick_ticker.updateEvent(ib._tick_ticker)
        assert len(depth) == 1
        assert isinstance(depth[0], DepthBook)
        assert list(depth[0].bids) == [(Decimal("100.0"), Decimal("5.0"))]
        assert trades == [tick]
        _feed(old_depth, _tick(1, 0, 1, 99.0, 6.0))  # the OLD ticker is detached
        assert len(depth) == 1

    async def test_1101_event_carries_code(self) -> None:
        transport, ib, _ = _make()
        await transport.connect()
        events: list[ConnectionStateEvent] = []
        transport.subscribe_connection_state(events.append)
        ib.errorEvent(-1, 1101, "lost")
        assert ConnectionStateEvent("data_lost", 1101, "lost") in events

    async def test_1102_does_not_resubscribe(self) -> None:
        transport, ib, _ = _make()
        states = _record_states(transport)
        await transport.connect()
        transport.subscribe("depth", "C", lambda t: None)
        ib.errorEvent(-1, 1100, "lost")
        ib.errorEvent(-1, 1102, "restored")
        assert len(ib.req_mkt_depth_calls) == 1
        assert ib._cancel_mkt_depth_calls == []
        assert states == ["connected", "disconnected", "connected"]

    async def test_closed_handle_is_not_resubscribed(self) -> None:
        """Issue #54 (D4): once released after the idle period, a closed handle stays gone."""
        transport, ib, _ = _make(IbConnectionSpec(depth_idle_release=0.05))
        await transport.connect()
        handle = transport.subscribe("depth", "C", lambda t: None)
        handle.close()
        handle.close()
        await asyncio.sleep(0.3)
        assert ib._cancel_mkt_depth_calls == ["C"]
        ib.errorEvent(-1, 1101, "lost")
        assert len(ib.req_mkt_depth_calls) == 1
        assert ib._cancel_mkt_depth_calls == ["C"]

    async def test_resubscribe_failure_is_logged_and_others_continue(self) -> None:
        transport, ib, _ = _make()
        await transport.connect()
        transport.subscribe("depth", "C1", lambda t: None)
        transport.subscribe("trades", "C2", lambda t: None)
        with patch.object(ib, "cancelMktDepth", side_effect=RuntimeError("nope")):
            transport.resubscribe_all()
        assert len(ib.req_tick_by_tick_calls) == 2

    def test_unknown_channel_rejected(self) -> None:
        transport, _, _ = _make()
        with pytest.raises(ValueError, match="Unknown subscribe channel"):
            transport.subscribe("bogus", "C", lambda t: None)


class TestReconnectSupervisor:
    async def test_drop_reconnects_with_capped_backoff_and_resubscribes(self) -> None:
        spec = IbConnectionSpec(reconnect_initial_delay=1.0, reconnect_max_delay=3.0)
        transport, ib, delays = _make(spec)
        states = _record_states(transport)
        await transport.connect()
        received: list[object] = []
        transport.subscribe("depth", "C", received.append)
        old_depth = ib._depth_ticker
        ib.rotate_tickers()
        fut = asyncio.get_running_loop().create_future()
        transport._error_router._pending_request_waiters[1] = fut

        ib.connect_failures = 2
        ib.simulate_drop()
        supervisor = transport._supervisor
        assert supervisor is not None
        # Pending waiters fail immediately and the router flips to disconnected.
        assert transport._error_router.is_connected is False
        with pytest.raises(ConnectionLostError):
            fut.result()

        await supervisor

        assert delays == [1.0, 2.0, 3.0]  # doubled, then capped at max
        assert states == [
            "connected",
            "disconnected",
            "reconnecting",
            "reconnecting",
            "reconnecting",
            "connected",
        ]
        assert transport._error_router.is_connected is True
        # Full reconnect: old stream is gone, so no cancel; request re-issued.
        assert ib._cancel_mkt_depth_calls == []
        assert len(ib.req_mkt_depth_calls) == 2
        assert old_depth.updateEvent._handlers == []
        _feed(ib._depth_ticker, _tick(1, 0, 0, 100.0, 5.0))
        assert len(received) == 1
        assert isinstance(received[0], DepthBook)
        assert list(received[0].bids) == [(Decimal("100.0"), Decimal("5.0"))]
        _feed(old_depth, _tick(1, 0, 1, 99.0, 6.0))  # the OLD ticker is detached
        assert len(received) == 1
        # Handlers were not double-attached across reconnects.
        assert len(ib.errorEvent._handlers) == 1
        assert len(ib.disconnectedEvent._handlers) == 1
        assert ib.market_data_type_calls == [3, 3]

    async def test_second_drop_event_does_not_spawn_second_supervisor(self) -> None:
        transport, ib, _ = _make()
        await transport.connect()
        ib.connect_failures = 1
        ib.simulate_drop()
        first = transport._supervisor
        ib.disconnectedEvent()  # e.g. fired by a failed reconnect attempt
        assert transport._supervisor is first
        await first

    async def test_supervisor_can_restart_after_successful_reconnect(self) -> None:
        transport, ib, _ = _make()
        await transport.connect()
        ib.simulate_drop()
        first = transport._supervisor
        await first
        ib.simulate_drop()
        assert transport._supervisor is not first
        await transport._supervisor
        assert len(ib.connect_calls) == 3

    async def test_disconnect_during_reconnect_cancels_cleanly(self) -> None:
        transport, ib, _ = _make()
        states = _record_states(transport)
        await transport.connect()
        gate = asyncio.Event()

        async def blocked_sleep(_delay: float) -> None:
            await gate.wait()

        transport._sleep = blocked_sleep
        ib.simulate_drop()
        supervisor = transport._supervisor
        await asyncio.sleep(0)

        await transport.disconnect()

        assert supervisor is not None
        assert supervisor.cancelled()
        assert transport._supervisor is None
        assert len(ib.connect_calls) == 1  # no reconnect attempt
        assert states == ["connected", "disconnected"]
        assert ib.errorEvent._handlers == []
        assert ib.disconnectedEvent._handlers == []
        assert ib.disconnect_calls == 1

    async def test_own_disconnect_does_not_start_supervisor(self) -> None:
        transport, ib, _ = _make()
        states = _record_states(transport)
        await transport.connect()
        await transport.disconnect()
        assert transport._supervisor is None
        assert len(ib.connect_calls) == 1
        assert states == ["connected"]
        assert transport._error_router.is_connected is False

    async def test_closing_flag_suppresses_supervisor(self) -> None:
        transport, ib, _ = _make()
        await transport.connect()
        transport._closing = True
        ib.simulate_drop()
        assert transport._supervisor is None

    async def test_connect_after_disconnect_rearms_supervisor(self) -> None:
        transport, ib, _ = _make()
        await transport.connect()
        await transport.disconnect()
        await transport.connect()
        assert len(ib.errorEvent._handlers) == 1
        ib.simulate_drop()
        assert transport._supervisor is not None
        await transport._supervisor


_TRANSPORT_LOGGER = "market_connector.exchanges.interactive_brokers.transport"


class TestServerInfo:
    def test_none_before_connect(self) -> None:
        transport, _, _ = _make()
        assert transport.server_info is None

    async def test_populated_after_connect(self) -> None:
        transport, _, _ = _make()
        await transport.connect()
        info = transport.server_info
        assert info == IbServerInfo(server_version=176, max_client_version=178)
        assert info is not None
        assert info.at_client_ceiling is False

    async def test_at_client_ceiling(self) -> None:
        transport, ib, _ = _make()
        ib.client.server_version = 178
        await transport.connect()
        assert transport.server_info is not None
        assert transport.server_info.at_client_ceiling is True

    def test_ceiling_unknown_max_is_false(self) -> None:
        assert IbServerInfo(server_version=200, max_client_version=None).at_client_ceiling is False

    async def test_connect_logs_server_version(self, caplog: pytest.LogCaptureFixture) -> None:
        transport, _, _ = _make()
        with caplog.at_level(logging.INFO, logger=_TRANSPORT_LOGGER):
            await transport.connect()
        records = [r for r in caplog.records if r.name == _TRANSPORT_LOGGER]
        assert any(
            r.levelno == logging.INFO and "server version 176" in r.getMessage() for r in records
        )

    async def test_reconnect_refreshes_and_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        transport, ib, _ = _make()
        await transport.connect()
        ib.client.server_version = 177
        with caplog.at_level(logging.INFO, logger=_TRANSPORT_LOGGER):
            ib.simulate_drop()
            # Retained across the drop: the supervisor has not run yet.
            assert transport.server_info == IbServerInfo(176, 178)
            supervisor = transport._supervisor
            assert supervisor is not None
            await supervisor
        assert transport.server_info == IbServerInfo(177, 178)
        assert any("server version 177" in r.getMessage() for r in caplog.records)

    async def test_retained_after_disconnect(self) -> None:
        transport, _, _ = _make()
        await transport.connect()
        await transport.disconnect()
        assert transport.server_info == IbServerInfo(176, 178)

    async def test_gateway_delegates(self) -> None:
        transport, _, _ = _make()
        gateway = IbGatewayGateway(transport=transport, signer=MagicMock())
        assert gateway.server_info is None
        await gateway.start()
        assert gateway.server_info == transport.server_info
        assert gateway.server_info == IbServerInfo(176, 178)


class TestConnectionStateTopic:
    async def test_handle_close_unsubscribes(self) -> None:
        transport, _, _ = _make()
        events: list[ConnectionStateEvent] = []
        handle = transport.subscribe_connection_state(events.append)
        assert handle.channel == CONNECTION_TOPIC
        await transport.connect()
        handle.close()
        handle.close()
        await transport.disconnect()
        await transport.connect()
        assert events == [ConnectionStateEvent("connected")]

    def test_event_is_frozen_with_defaults(self) -> None:
        event = ConnectionStateEvent("connected")
        assert (event.code, event.message) == (None, "")
        with pytest.raises(AttributeError):
            event.state = "x"  # type: ignore[misc]

    async def test_gateway_passthrough(self) -> None:
        transport, _, _ = _make()
        gateway = IbGatewayGateway(transport=transport, signer=MagicMock())
        events: list[ConnectionStateEvent] = []
        gateway.subscribe_connection_state(events.append)
        await gateway.start()
        assert [e.state for e in events] == ["connected"]
