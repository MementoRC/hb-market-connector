"""Acceptance tests for the connect timeout and initial-connect retry (issue #103).

All run against MockIb; ``connectAsync`` is scripted per test with a list of
outcomes (an exception to raise, or None to succeed).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from market_connector.exchanges.interactive_brokers.exceptions import IbAccountConfigError
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec

from .test_connection_lifecycle import _make, _record_states

if TYPE_CHECKING:
    from collections.abc import Callable

    from ._doubles import MockIb


def _sync_timeout() -> ConnectionError:
    """The shape ib_async raises for a sync-request timeout during connect."""
    return ConnectionError(["executions request timed out"])


def _script(
    ib: MockIb, outcomes: list[BaseException | None], before_raise: Callable[[], None] | None = None
) -> None:
    """Make successive connectAsync calls raise/succeed per ``outcomes``."""
    queue = list(outcomes)

    async def connect_async(**kwargs: object) -> None:
        ib.call_order.append("connectAsync")
        ib.connect_calls.append(kwargs)
        outcome = queue.pop(0)
        if outcome is not None:
            if before_raise is not None:
                before_raise()
            raise outcome
        ib.connected = True

    ib.connectAsync = connect_async  # type: ignore[method-assign]


class TestSpecFields:
    def test_defaults(self) -> None:
        spec = IbConnectionSpec()
        assert spec.connect_timeout == 15.0
        assert spec.connect_attempts == 3

    @pytest.mark.parametrize("timeout", [None, 0, 0.0])
    def test_no_timeout_values_accepted(self, timeout: float | None) -> None:
        assert IbConnectionSpec(connect_timeout=timeout).connect_timeout == timeout

    def test_negative_timeout_rejected(self) -> None:
        with pytest.raises(ValueError, match="connect_timeout"):
            IbConnectionSpec(connect_timeout=-1.0)

    def test_zero_attempts_rejected(self) -> None:
        with pytest.raises(ValueError, match="connect_attempts"):
            IbConnectionSpec(connect_attempts=0)


class TestTimeoutPassthrough:
    async def test_default_timeout_passed_to_connect_async(self) -> None:
        transport, ib, _ = _make()
        await transport.connect()
        assert ib.connect_calls[0]["timeout"] == 15.0

    async def test_custom_timeout_passed_to_connect_async(self) -> None:
        transport, ib, _ = _make(IbConnectionSpec(connect_timeout=30.0))
        await transport.connect()
        assert ib.connect_calls[0]["timeout"] == 30.0


class TestInitialRetry:
    async def test_sync_timeout_then_success(self) -> None:
        transport, ib, delays = _make()
        states = _record_states(transport)
        _script(ib, [_sync_timeout(), None])
        await transport.connect()
        assert len(ib.connect_calls) == 2
        assert delays == [IbConnectionSpec().reconnect_initial_delay]
        assert transport._supervisor is None
        assert states.count("connected") == 1

    async def test_timeout_error_then_success(self) -> None:
        transport, ib, _ = _make()
        _script(ib, [TimeoutError(), None])
        await transport.connect()
        assert len(ib.connect_calls) == 2
        assert transport.is_connected is True

    async def test_backoff_doubles_and_caps_at_max_delay(self) -> None:
        spec = IbConnectionSpec(
            connect_attempts=4, reconnect_initial_delay=1.0, reconnect_max_delay=3.0
        )
        transport, ib, delays = _make(spec)
        _script(ib, [TimeoutError(), TimeoutError(), TimeoutError(), None])
        await transport.connect()
        assert delays == [1.0, 2.0, 3.0]

    @pytest.mark.parametrize(
        "error",
        [
            ConnectionRefusedError("refused"),
            IbAccountConfigError("bad account"),
            ConnectionError("Socket connection broken while connecting"),
            RuntimeError("boom"),
        ],
    )
    async def test_non_retryable_errors_raise_immediately(self, error: BaseException) -> None:
        transport, ib, delays = _make()
        _script(ib, [error, None])
        with pytest.raises(type(error)):
            await transport.connect()
        assert len(ib.connect_calls) == 1
        assert delays == []

    async def test_all_attempts_fail_raises_last_error(self) -> None:
        transport, ib, _ = _make(IbConnectionSpec(connect_attempts=2))
        states = _record_states(transport)
        last = TimeoutError("second")
        _script(ib, [TimeoutError("first"), last])
        with pytest.raises(TimeoutError) as excinfo:
            await transport.connect()
        assert excinfo.value is last
        assert len(ib.connect_calls) == 2
        assert transport._supervisor is None
        assert "connected" not in states

    async def test_disconnected_event_during_retry_does_not_start_supervisor(self) -> None:
        transport, ib, _ = _make()

        def fail_attempt_disconnect() -> None:
            transport._attach_handlers()
            ib.disconnectedEvent()

        _script(ib, [_sync_timeout(), None], before_raise=fail_attempt_disconnect)
        await transport.connect()
        assert transport._supervisor is None
        assert len(ib.connect_calls) == 2

    async def test_single_attempt_does_not_retry(self) -> None:
        transport, ib, delays = _make(IbConnectionSpec(connect_attempts=1))
        _script(ib, [_sync_timeout(), None])
        with pytest.raises(ConnectionError):
            await transport.connect()
        assert len(ib.connect_calls) == 1
        assert delays == []
