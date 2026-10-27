"""Acceptance tests for version-refusal fail-fast (issue #142).

ib_async 2.1.0 signals a TWS/Gateway version refusal only through
``ib.client.apiError(msg)``; ``connectAsync`` then fails with a bare ``TimeoutError``.
The transport must turn that into ``IbVersionIncompatibleError`` without retrying.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from market_connector.exchanges.interactive_brokers.exceptions import IbVersionIncompatibleError
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec

from .test_connect_retry import _script
from .test_connection_lifecycle import _make, _record_states

if TYPE_CHECKING:
    from market_connector.exchanges.interactive_brokers.transport import ConnectionStateEvent

    from ._doubles import MockIb

_LOGGER = "market_connector.exchanges.interactive_brokers.transport"
_REFUSAL = "TWS/gateway version must be >= 972"


def _refuse(ib: MockIb, text: str = _REFUSAL) -> None:
    """Script every connect attempt to emit ``text`` on apiError, then raise TimeoutError."""
    _script(ib, [TimeoutError()] * 10, before_raise=lambda: ib.client.apiError.emit(text))


class TestInitialConnect:
    async def test_refusal_fails_fast_without_retry(self) -> None:
        transport, ib, delays = _make()
        _refuse(ib)
        with pytest.raises(IbVersionIncompatibleError) as excinfo:
            await transport.connect()
        assert len(ib.connect_calls) == 1
        assert delays == []
        assert _REFUSAL in str(excinfo.value)
        assert "#59" in str(excinfo.value)

    async def test_refusal_match_is_case_insensitive(self) -> None:
        transport, ib, _ = _make()
        _refuse(ib, "TWS/Gateway VERSION MUST BE >= 972")
        with pytest.raises(IbVersionIncompatibleError):
            await transport.connect()
        assert len(ib.connect_calls) == 1

    async def test_timeout_without_api_error_still_retried_and_repr_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        spec = IbConnectionSpec()
        transport, ib, _ = _make(spec)
        _script(ib, [TimeoutError()] * spec.connect_attempts)
        with caplog.at_level(logging.WARNING, logger=_LOGGER), pytest.raises(TimeoutError):
            await transport.connect()
        assert len(ib.connect_calls) == spec.connect_attempts
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("TimeoutError()" in r.getMessage() for r in warnings)

    async def test_unrelated_api_error_is_still_retried(self) -> None:
        spec = IbConnectionSpec()
        transport, ib, _ = _make(spec)
        _script(
            ib,
            [TimeoutError()] * spec.connect_attempts,
            before_raise=lambda: ib.client.apiError.emit(
                "Peer closed connection. clientId 1 already in use?"
            ),
        )
        with pytest.raises(TimeoutError) as excinfo:
            await transport.connect()
        assert not isinstance(excinfo.value, IbVersionIncompatibleError)
        assert len(ib.connect_calls) == spec.connect_attempts

    async def test_api_error_subscription_scoped_to_connect_attempts(self) -> None:
        transport, ib, _ = _make()
        seen_during: list[int] = []
        baseline = len(ib.client.apiError._handlers)

        async def connect_async(**kwargs: object) -> None:
            ib.connect_calls.append(kwargs)
            seen_during.append(len(ib.client.apiError._handlers))
            ib.connected = True

        ib.connectAsync = connect_async  # type: ignore[method-assign]
        await transport.connect()
        assert seen_during == [baseline + 1]
        assert len(ib.client.apiError._handlers) == baseline

    async def test_api_error_subscription_removed_after_refusal(self) -> None:
        transport, ib, _ = _make()
        baseline = len(ib.client.apiError._handlers)
        _refuse(ib)
        with pytest.raises(IbVersionIncompatibleError):
            await transport.connect()
        assert len(ib.client.apiError._handlers) == baseline


class TestReconnectSupervisor:
    async def test_refused_reconnect_stops_and_publishes_disconnected(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        transport, ib, _ = _make()
        states = _record_states(transport)
        events: list[ConnectionStateEvent] = []
        transport.subscribe_connection_state(events.append)
        _script(
            ib,
            [None] + [TimeoutError()] * 10,
            before_raise=lambda: ib.client.apiError.emit(_REFUSAL),
        )
        await transport.connect()

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            ib.simulate_drop()
            supervisor = transport._supervisor
            assert supervisor is not None
            await supervisor

        assert len(ib.connect_calls) == 2  # initial connect + the one refused reconnect
        assert any(r.levelno == logging.ERROR and r.name == _LOGGER for r in caplog.records)
        assert states[-1] == "disconnected"
        assert _REFUSAL in events[-1].message

    async def test_reconnect_warning_uses_repr(self, caplog: pytest.LogCaptureFixture) -> None:
        transport, ib, _ = _make()
        _script(ib, [None, TimeoutError(), None])
        await transport.connect()
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            ib.simulate_drop()
            supervisor = transport._supervisor
            assert supervisor is not None
            await supervisor
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("TimeoutError()" in r.getMessage() for r in warnings)
