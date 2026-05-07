"""Tests for _ErrorRouter — IB error code dispatch to pending call futures."""

from __future__ import annotations

import asyncio

import pytest

from market_connector.exchanges.interactive_brokers._error_router import _ErrorRouter
from market_connector.exchanges.interactive_brokers.exceptions import (
    ConnectionLostError,
    ConnectionTerminatedError,
    ContractNotFoundError,
    OrderRejectedError,
)


class TestErrorTable:
    @pytest.mark.parametrize(
        "code,exc_cls",
        [
            (162, ContractNotFoundError),
            (200, ContractNotFoundError),
            (201, OrderRejectedError),
            (321, OrderRejectedError),
            (325, OrderRejectedError),
            (1100, ConnectionLostError),
            (1300, ConnectionTerminatedError),
        ],
    )
    def test_mapped_codes_route_correctly(self, code, exc_cls):
        router = _ErrorRouter()
        loop = asyncio.get_event_loop()
        if exc_cls in (ContractNotFoundError,):
            fut = loop.create_future()
            router._pending_request_waiters[1] = fut
            router.on_error(req_id=1, code=code, msg="test")
            assert fut.done()
            with pytest.raises(exc_cls):
                fut.result()
        elif exc_cls is OrderRejectedError:
            fut = loop.create_future()
            router._pending_order_waiters[1] = fut
            router.on_error(req_id=1, code=code, msg="test")
            assert fut.done()
            with pytest.raises(exc_cls):
                fut.result()
        elif exc_cls is ConnectionLostError:
            req_fut = loop.create_future()
            ord_fut = loop.create_future()
            router._pending_request_waiters[1] = req_fut
            router._pending_order_waiters[2] = ord_fut
            router.on_error(req_id=-1, code=code, msg="test")
            assert router.is_connected is False
            assert req_fut.done()
            assert ord_fut.done()
        elif exc_cls is ConnectionTerminatedError:
            req_fut = loop.create_future()
            router._pending_request_waiters[1] = req_fut
            router.on_error(req_id=-1, code=code, msg="test")
            assert req_fut.done()
            with pytest.raises(exc_cls):
                req_fut.result()

    def test_unrouted_code_does_not_raise(self, caplog):
        import logging

        router = _ErrorRouter()
        with caplog.at_level(logging.DEBUG):
            router.on_error(req_id=1, code=9999, msg="some unknown IB error")
        # Should log at debug, not raise
        assert any("9999" in r.message for r in caplog.records)


class TestConnectionState:
    def test_initial_state_is_connected(self):
        router = _ErrorRouter()
        assert router.is_connected is True

    def test_1100_sets_disconnected(self):
        router = _ErrorRouter()
        router.on_error(req_id=-1, code=1100, msg="connectivity lost")
        assert router.is_connected is False

    def test_1102_restores_connected(self):
        router = _ErrorRouter()
        router.on_error(req_id=-1, code=1100, msg="connectivity lost")
        assert router.is_connected is False
        router.on_error(req_id=-1, code=1102, msg="connectivity restored")
        assert router.is_connected is True

    def test_1300_does_not_flip_connected(self):
        """ConnectionTerminated fails all pending but does not change is_connected."""
        router = _ErrorRouter()
        router.on_error(req_id=-1, code=1300, msg="terminated")
        # is_connected stays True — explicit reconnect is required, but we don't
        # flip the flag on termination (only on 1100)
        assert router.is_connected is True

    def test_connection_listener_notified_on_1100(self):
        router = _ErrorRouter()
        events: list[bool] = []
        router._connection_listeners.append(events.append)
        router.on_error(req_id=-1, code=1100, msg="lost")
        assert events == [False]

    def test_connection_listener_notified_on_1102(self):
        router = _ErrorRouter()
        events: list[bool] = []
        router._connection_listeners.append(events.append)
        router.on_error(req_id=-1, code=1100, msg="lost")
        router.on_error(req_id=-1, code=1102, msg="restored")
        assert events == [False, True]


class TestFailAllPending:
    def test_1100_fails_all_pending_futures(self):
        loop = asyncio.get_event_loop()
        router = _ErrorRouter()
        req_fut = loop.create_future()
        ord_fut = loop.create_future()
        router._pending_request_waiters[10] = req_fut
        router._pending_order_waiters[20] = ord_fut

        router.on_error(req_id=-1, code=1100, msg="lost")

        assert req_fut.done()
        assert ord_fut.done()
        with pytest.raises(ConnectionLostError):
            req_fut.result()
        with pytest.raises(ConnectionLostError):
            ord_fut.result()
        # Registries cleared
        assert len(router._pending_request_waiters) == 0
        assert len(router._pending_order_waiters) == 0
