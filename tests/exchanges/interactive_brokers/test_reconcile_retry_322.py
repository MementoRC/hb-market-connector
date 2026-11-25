"""RED acceptance tests for #322 (Interactive Brokers): retry/backoff of ``_reconcile_executions``.

Contract pinned here (none of it exists yet):

* ``IbGatewayTransport._reconcile_max_attempts`` (int, total attempts incl. the first, default >= 2)
  and ``IbGatewayTransport._reconcile_backoff_s`` (float, base delay in seconds between attempts)
  are plain instance attributes, overridable after construction and before ``connect()``.
  Backoff is awaited through ``asyncio.sleep`` (patchable) and may grow between attempts.
* A failed attempt (``reqExecutionsAsync`` raising, or timing out) is retried until it succeeds or
  ``_reconcile_max_attempts`` is reached. Fills of the successful attempt are ingested and emitted
  through the normal ``LegFill`` path.
* After the last failed attempt the transport gives up cleanly: nothing escapes ``connect()`` or the
  reconnect supervisor, the transport stays connected/usable, and a WARNING or ERROR record whose
  rendered text contains "giving up" (case-insensitive) is logged via the transport module logger.
* A first-attempt success calls ``reqExecutionsAsync`` exactly once and never sleeps for backoff.

Runs without ib_async: a stub ``ib_async.ExecutionFilter`` is injected for the lazy import.
"""

from __future__ import annotations

import asyncio
import sys
import types
from decimal import Decimal
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_connector.exchanges.interactive_brokers.ib_gateway import IbGatewayGateway
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.transport import IbGatewayTransport

from ._doubles import MockIb, make_fill

if TYPE_CHECKING:
    from market_connector.protocols import LegFill

_IB_PATH = "market_connector.exchanges.interactive_brokers.transport.IB"
_LOGGER_PATH = "market_connector.exchanges.interactive_brokers.transport.logger"
ACCT = "DU1111111"
TAG = "mcl-7-0"
MAX_ATTEMPTS = 3
BACKOFF_S = 0.001


class FlakyIb(MockIb):
    """MockIb whose reqExecutionsAsync fails the first ``fail_times`` calls."""

    def __init__(self, fail_times: int, exc: Exception | None = None) -> None:
        super().__init__()
        self.fail_times = fail_times
        self.exc = exc if exc is not None else ConnectionError("reqExecutions boom")
        self.calls = 0

    async def reqExecutionsAsync(self, execFilter: object = None) -> list[object]:  # noqa: N802, N803
        self.calls += 1
        self.req_executions_filters.append(execFilter)
        if self.calls <= self.fail_times:
            raise self.exc
        return list(self.executions_result)


@pytest.fixture(autouse=True)
def _stub_ib_async(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = types.ModuleType("ib_async")
    stub.ExecutionFilter = lambda **kw: SimpleNamespace(**kw)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ib_async", stub)


def _transport(ib: MockIb) -> IbGatewayTransport:
    ib.managed_accounts = [ACCT]
    with patch(_IB_PATH, return_value=ib):
        transport = IbGatewayTransport(IbConnectionSpec(account_id=ACCT))
    transport._reconcile_max_attempts = MAX_ATTEMPTS  # noqa: SLF001
    transport._reconcile_backoff_s = BACKOFF_S  # noqa: SLF001
    return transport


def _gateway(transport: IbGatewayTransport) -> IbGatewayGateway:
    resolver = MagicMock()
    resolver.resolve = AsyncMock(return_value=SimpleNamespace(native=SimpleNamespace(conId=1)))
    return IbGatewayGateway(transport=transport, signer=MagicMock(), contract_resolver=resolver)


def _missed_fill() -> Any:
    return make_fill("m1.01", order_id=0, cum_qty=10, shares=10, order_ref=TAG)


def _rendered(logger: MagicMock) -> list[str]:
    out: list[str] = []
    for method in ("warning", "error", "exception", "critical"):
        for call in getattr(logger, method).call_args_list:
            msg, *args = call.args
            out.append((msg % tuple(args)) if args else str(msg))
    return out


class TestRetryThenSucceed:
    async def test_second_attempt_succeeds_and_fill_is_emitted(self) -> None:
        ib = FlakyIb(fail_times=1)
        ib.executions_result = [_missed_fill()]
        transport = _transport(ib)
        got: list[LegFill] = []
        _gateway(transport).subscribe_leg_fills(got.append)

        await transport.connect()

        assert ib.calls == 2
        assert [(f.client_order_id, f.cumulative_filled) for f in got] == [(TAG, Decimal(10))]

    async def test_last_allowed_attempt_succeeds(self) -> None:
        ib = FlakyIb(fail_times=MAX_ATTEMPTS - 1)
        ib.executions_result = [_missed_fill()]
        transport = _transport(ib)
        got: list[LegFill] = []
        _gateway(transport).subscribe_leg_fills(got.append)

        await transport.connect()

        assert ib.calls == MAX_ATTEMPTS
        assert [f.cumulative_filled for f in got] == [Decimal(10)]

    async def test_timeout_is_retried_like_an_error(self) -> None:
        ib = FlakyIb(fail_times=1, exc=TimeoutError())
        ib.executions_result = [_missed_fill()]
        transport = _transport(ib)
        got: list[LegFill] = []
        _gateway(transport).subscribe_leg_fills(got.append)

        await transport.connect()

        assert ib.calls == 2
        assert [f.cumulative_filled for f in got] == [Decimal(10)]

    async def test_reconnect_reconcile_is_retried_and_emits(self) -> None:
        ib = FlakyIb(fail_times=0)
        transport = _transport(ib)
        got: list[LegFill] = []
        _gateway(transport).subscribe_leg_fills(got.append)
        await transport.connect()
        ib.calls = 0
        ib.fail_times = 1
        ib.executions_result = [_missed_fill()]

        ib.simulate_drop()
        assert transport._supervisor is not None  # noqa: SLF001
        await transport._supervisor  # noqa: SLF001

        assert ib.calls == 2
        assert [f.cumulative_filled for f in got] == [Decimal(10)]


class TestBackoff:
    async def test_backoff_is_awaited_between_attempts_not_before_first(self) -> None:
        ib = FlakyIb(fail_times=2)
        transport = _transport(ib)
        real_sleep = asyncio.sleep
        delays: list[float] = []

        async def spy(delay: float, *a: Any, **kw: Any) -> None:
            delays.append(delay)
            await real_sleep(0)

        with patch("asyncio.sleep", spy):
            await transport.connect()

        backoffs = [d for d in delays if d >= BACKOFF_S]
        assert ib.calls == 3
        assert len(backoffs) == 2
        assert backoffs == sorted(backoffs)

    async def test_a_backoff_sleep_precedes_every_retry_request(self) -> None:
        ib = FlakyIb(fail_times=2)
        transport = _transport(ib)
        real_sleep = asyncio.sleep
        calls_at_sleep: list[int] = []

        async def spy(delay: float, *a: Any, **kw: Any) -> None:
            if delay >= BACKOFF_S:
                calls_at_sleep.append(ib.calls)
            await real_sleep(0)

        with patch("asyncio.sleep", spy):
            await transport.connect()

        # One backoff after the 1st failed request and one after the 2nd, none before the 1st.
        assert calls_at_sleep == [1, 2]


class TestSocketDropDuringReconcile:
    async def test_drop_during_reconcile_is_retried_not_reported_connected(self) -> None:
        ib = FlakyIb(fail_times=0)
        transport = _transport(ib)
        await transport.connect()
        ib.calls = 0
        original = ib.reqExecutionsAsync

        async def drop_once(execFilter: object = None) -> list[object]:  # noqa: N803
            result = await original(execFilter)
            if ib.calls == 1:
                ib.connected = False  # socket dies mid-reconcile; supervisor swallows the event
            return result

        ib.reqExecutionsAsync = drop_once  # type: ignore[method-assign]
        ib.simulate_drop()
        assert transport._supervisor is not None  # noqa: SLF001
        await transport._supervisor  # noqa: SLF001

        assert ib.calls == 2
        assert transport.is_connected


class TestGiveUp:
    async def test_gives_up_after_max_attempts_without_raising(self) -> None:
        ib = FlakyIb(fail_times=10_000)
        transport = _transport(ib)

        await transport.connect()  # must not raise

        assert ib.calls == MAX_ATTEMPTS

    async def test_gives_up_with_warning_or_error_log_naming_it(self) -> None:
        ib = FlakyIb(fail_times=10_000)
        transport = _transport(ib)

        with patch(_LOGGER_PATH) as logger:
            await transport.connect()

        assert any("giving up" in m.lower() for m in _rendered(logger))

    async def test_transport_stays_usable_after_give_up(self) -> None:
        ib = FlakyIb(fail_times=10_000)
        transport = _transport(ib)

        await transport.connect()

        assert transport.is_connected
        assert transport._supervisor is None or not transport._supervisor.done()  # noqa: SLF001

    async def test_give_up_does_not_block_later_fills_on_live_stream(self) -> None:
        ib = FlakyIb(fail_times=10_000)
        transport = _transport(ib)
        got: list[LegFill] = []
        _gateway(transport).subscribe_leg_fills(got.append)
        await transport.connect()

        ib.execDetailsEvent(None, _missed_fill())

        assert [f.cumulative_filled for f in got] == [Decimal(10)]


class TestFirstAttemptSuccess:
    async def test_single_call_and_no_backoff_sleep(self) -> None:
        ib = FlakyIb(fail_times=0)
        transport = _transport(ib)
        transport._reconcile_backoff_s = 5.0  # noqa: SLF001
        real_sleep = asyncio.sleep
        delays: list[float] = []

        async def spy(delay: float, *a: Any, **kw: Any) -> None:
            delays.append(delay)
            await real_sleep(0)

        with patch("asyncio.sleep", spy):
            await transport.connect()

        assert ib.calls == 1
        assert all(d < 5.0 for d in delays)
        # The first backoff would be exactly the base delay; none may be requested.
        assert 5.0 not in delays

    async def test_first_attempt_fills_emitted_once(self) -> None:
        ib = FlakyIb(fail_times=0)
        ib.executions_result = [_missed_fill()]
        transport = _transport(ib)
        got: list[LegFill] = []
        _gateway(transport).subscribe_leg_fills(got.append)

        await transport.connect()

        assert ib.calls == 1
        assert [f.cumulative_filled for f in got] == [Decimal(10)]


class TestDefaults:
    async def test_default_knobs_allow_retry_and_have_positive_backoff(self) -> None:
        with patch(_IB_PATH, return_value=MockIb()):
            transport = IbGatewayTransport(IbConnectionSpec(account_id=ACCT))

        assert transport._reconcile_max_attempts >= 2  # noqa: SLF001
        assert transport._reconcile_backoff_s > 0  # noqa: SLF001
