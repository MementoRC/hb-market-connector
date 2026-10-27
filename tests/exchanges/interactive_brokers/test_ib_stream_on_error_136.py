"""Acceptance tests for issue #136: IB stream errors reach every depth subscriber.

``subscribe("depth", contract, callback, *, on_error=None)`` shares one reqMktDepth stream (and one
market-data line) between all subscribers of a contract. Today a fatal IB error on that reqId is
only logged, so subscribers never learn of it and the line stays counted.

Pinned decisions (same API as #129 for trades):
- A fatal code (101 -> ``MarketDataLinesExhaustedError``, 354 -> ``MarketDataPermissionError``) on
  the stream's reqId fails the whole shared stream: every subscriber handle is closed, gets
  ``handle.error`` set and its own ``on_error(handle.error)`` called exactly once.
- The shared line is released exactly once and the depth stream is cancelled exactly once.
- A later ``handle.close()`` is a no-op; a repeated error for the same reqId is not re-delivered.
- An exception raised by an ``on_error`` callback is logged, never raised into the injector, and
  does not stop the other subscribers from being notified.
- Soft codes (2152) and unknown reqIds leave the handles, stream and line untouched.
- After a failure a new depth subscribe on the same contract opens a fresh stream.

The L1 (``top_of_book``) fatal-error case is already covered by
``test_ac7_error_101_fails_top_of_book_and_frees_line`` in ``test_market_data_line_cap_104.py``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import pytest

from market_connector.exchanges.interactive_brokers.exceptions import (
    MarketDataLinesExhaustedError,
    MarketDataPermissionError,
)

from .test_market_data_line_cap_104 import _MSG_101, _started
from .test_persistent_market_data import FakeContract, Rig

if TYPE_CHECKING:
    from collections.abc import Callable

_PROMPT = 2.0
_SETTLE = 0.05
_MSG_354 = "Requested market data is not subscribed."
_FATAL = [(101, MarketDataLinesExhaustedError), (354, MarketDataPermissionError)]
_MSG_BY_CODE = {101: _MSG_101, 354: _MSG_354}


class _Recorder:
    """Records data-callback deliveries and on_error invocations of one subscription."""

    def __init__(self) -> None:
        self.data: list[Any] = []
        self.errors: list[Exception] = []

    def __call__(self, data: Any) -> None:
        self.data.append(data)

    def on_error(self, exc: Exception) -> None:
        self.errors.append(exc)


async def _wait_until(predicate: Callable[[], bool]) -> None:
    for _ in range(int(_PROMPT / 0.005)):
        if predicate():
            return
        await asyncio.sleep(0.005)


def _depth_cancels(rig: Rig) -> int:
    return len(rig.ib._cancel_mkt_depth_calls)


def _depth_reqs(rig: Rig) -> int:
    return len(rig.ib.req_mkt_depth_calls)


def _inject(rig: Rig, req_id: int, code: int) -> None:
    rig.ib.errorEvent(req_id, code, _MSG_BY_CODE.get(code, _MSG_101), None)


async def _open_pair(rig: Rig, con_id: int = 1) -> tuple[Any, Any, _Recorder, _Recorder]:
    contract = FakeContract(con_id)
    rec_a, rec_b = _Recorder(), _Recorder()
    handle_a = rig.transport.subscribe("depth", contract, rec_a, on_error=rec_a.on_error)
    handle_b = rig.transport.subscribe("depth", contract, rec_b, on_error=rec_b.on_error)
    assert rig.transport.market_data_lines_in_use == 1
    assert len(rig.ib._depth_req_ids) == 1
    return handle_a, handle_b, rec_a, rec_b


@pytest.mark.parametrize(("code", "expected"), _FATAL)
async def test_fatal_depth_error_fails_every_subscriber_and_releases_line_once(
    code: int, expected: type[Exception]
) -> None:
    rig = await _started(market_data_lines=1)
    handle_a, handle_b, rec_a, rec_b = await _open_pair(rig)

    _inject(rig, rig.ib._depth_req_ids[0], code)
    await _wait_until(lambda: handle_a.closed and handle_b.closed)

    assert handle_a.closed is True
    assert handle_b.closed is True
    assert isinstance(handle_a.error, expected), repr(handle_a.error)
    assert isinstance(handle_b.error, expected), repr(handle_b.error)
    assert rec_a.errors == [handle_a.error]
    assert rec_b.errors == [handle_b.error]
    assert rig.transport.market_data_lines_in_use == 0
    assert _depth_cancels(rig) == 1


@pytest.mark.parametrize("code", [101, 354])
async def test_close_after_fatal_depth_error_is_noop(code: int) -> None:
    rig = await _started(market_data_lines=1)
    handle_a, handle_b, rec_a, rec_b = await _open_pair(rig)
    _inject(rig, rig.ib._depth_req_ids[0], code)
    await _wait_until(lambda: handle_a.closed and handle_b.closed)

    handle_a.close()
    handle_b.close()
    handle_a.close()

    assert rig.transport.market_data_lines_in_use == 0
    assert _depth_cancels(rig) == 1
    assert len(rec_a.errors) == 1
    assert len(rec_b.errors) == 1


@pytest.mark.parametrize("code", [101, 354])
async def test_fatal_depth_error_delivered_once(code: int) -> None:
    rig = await _started(market_data_lines=1)
    handle_a, handle_b, rec_a, rec_b = await _open_pair(rig)
    req_id = rig.ib._depth_req_ids[0]

    _inject(rig, req_id, code)
    await _wait_until(lambda: handle_a.closed and handle_b.closed)
    _inject(rig, req_id, code)
    await asyncio.sleep(_SETTLE)

    assert len(rec_a.errors) == 1
    assert len(rec_b.errors) == 1
    assert _depth_cancels(rig) == 1
    assert rig.transport.market_data_lines_in_use == 0


async def test_depth_on_error_exception_is_logged_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = await _started(market_data_lines=1)
    contract = FakeContract(1)
    bad_calls: list[Exception] = []

    def raising_on_error(exc: Exception) -> None:
        bad_calls.append(exc)
        raise RuntimeError("callback boom")

    rec_ok = _Recorder()
    bad = rig.transport.subscribe("depth", contract, _Recorder(), on_error=raising_on_error)
    ok = rig.transport.subscribe("depth", contract, rec_ok, on_error=rec_ok.on_error)

    with caplog.at_level(logging.ERROR):
        try:
            _inject(rig, rig.ib._depth_req_ids[0], 101)
        except Exception as exc:  # noqa: BLE001 - the injector must never see this
            pytest.fail(f"error injection raised into the caller: {exc!r}")
        await _wait_until(lambda: ok.closed and bad.closed)

    assert len(bad_calls) == 1
    assert len(rec_ok.errors) == 1
    assert isinstance(ok.error, MarketDataLinesExhaustedError)
    assert rec_ok.errors == [ok.error]
    assert any(r.levelno >= logging.ERROR and "on_error" in r.getMessage() for r in caplog.records)


async def test_soft_permission_code_keeps_depth_handles_open() -> None:
    rig = await _started(market_data_lines=1)
    handle_a, handle_b, rec_a, rec_b = await _open_pair(rig)

    _inject(rig, rig.ib._depth_req_ids[0], 2152)
    await asyncio.sleep(_SETTLE)

    assert handle_a.closed is False
    assert handle_b.closed is False
    assert handle_a.error is None
    assert handle_b.error is None
    assert rec_a.errors == []
    assert rec_b.errors == []
    assert rig.transport.market_data_lines_in_use == 1
    assert _depth_cancels(rig) == 0
    handle_a.close()
    handle_b.close()


async def test_error_for_unknown_reqid_is_ignored() -> None:
    rig = await _started(market_data_lines=1)
    handle_a, handle_b, rec_a, rec_b = await _open_pair(rig)

    _inject(rig, 99999, 101)
    await asyncio.sleep(_SETTLE)

    assert handle_a.closed is False
    assert handle_b.closed is False
    assert handle_a.error is None
    assert handle_b.error is None
    assert rec_a.errors == []
    assert rec_b.errors == []
    assert rig.transport.market_data_lines_in_use == 1
    assert _depth_cancels(rig) == 0
    handle_a.close()
    handle_b.close()


async def test_new_depth_subscribe_after_failure_opens_fresh_stream() -> None:
    rig = await _started(market_data_lines=1)
    handle_a, handle_b, _rec_a, _rec_b = await _open_pair(rig)
    _inject(rig, rig.ib._depth_req_ids[0], 101)
    await _wait_until(lambda: handle_a.closed and handle_b.closed)

    fresh = rig.transport.subscribe("depth", FakeContract(1), _Recorder())

    assert rig.transport.market_data_lines_in_use == 1
    assert _depth_reqs(rig) == 2
    assert len(rig.ib._depth_req_ids) == 2
    fresh.close()
