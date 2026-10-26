"""Acceptance tests for issue #129: IB error 101 (and hard permission codes) on trades streams.

``subscribe("trades")`` issues ``reqTickByTickData`` and counts one market-data line. Today the
reqId of that request is not tracked, so an IB error on it is only logged: the subscriber never
learns of it and the line stays counted until the handle closes.

Decided API:
- ``subscribe(channel, contract, callback, *, on_error=None)``: optional keyword-only error
  callback.
- ``SubscriptionHandle`` has public ``error: Exception | None`` (default None) and read-only
  ``closed``.
- On 101 or a hard permission code (354) for the stream's reqId the transport releases the line
  exactly once, closes the handle, sets ``handle.error`` to ``MarketDataLinesExhaustedError``
  (101) / ``MarketDataPermissionError`` (hard code) and calls ``on_error(handle.error)`` once.
- The data callback never receives an exception.
- A later ``handle.close()`` is a no-op: no second release, no second ``on_error``.
- Soft permission codes and unknown reqIds leave the handle, stream and line untouched.
"""

from __future__ import annotations

import asyncio
from typing import Any

from market_connector.exchanges.interactive_brokers.exceptions import (
    MarketDataLinesExhaustedError,
    MarketDataPermissionError,
)

from .test_market_data_line_cap_104 import _MSG_101, _started
from .test_market_data_real_contract import _resolved
from .test_persistent_market_data import FakeContract, Rig

_PROMPT = 2.0
_MSG_354 = "Requested market data is not subscribed."
_MSG_2152 = "Exchange is not subscribed; delayed data available."


class _Collector:
    """Records data-callback deliveries and on_error invocations of one subscription."""

    def __init__(self) -> None:
        self.data: list[Any] = []
        self.errors: list[Exception] = []

    def __call__(self, data: Any) -> None:
        self.data.append(data)

    def on_error(self, exc: Exception) -> None:
        self.errors.append(exc)

    def assert_data_has_no_exceptions(self) -> None:
        assert not [d for d in self.data if isinstance(d, BaseException)]


def _open(rig: Rig, collector: _Collector, con_id: int = 1, *, with_on_error: bool = True) -> Any:
    kwargs: dict[str, Any] = {"on_error": collector.on_error} if with_on_error else {}
    handle = rig.transport.subscribe("trades", _resolved(con_id), collector, **kwargs)
    assert rig.ib._tick_req_ids, "FakeIB did not assign a reqId to reqTickByTickData"
    return handle


async def _wait_failed(handle: Any) -> Exception:
    for _ in range(int(_PROMPT / 0.005)):
        if handle.error is not None:
            return handle.error
        await asyncio.sleep(0.005)
    raise AssertionError("handle.error was never set")


def _cancels(rig: Rig) -> int:
    return len(rig.ib._cancel_tick_by_tick_calls)


async def test_error_101_on_trades_reqid_frees_line_and_sets_typed_error() -> None:
    rig = await _started(market_data_lines=1)
    collector = _Collector()
    handle = _open(rig, collector)
    assert rig.transport.market_data_lines_in_use == 1
    assert handle.error is None
    assert handle.closed is False

    rig.ib.errorEvent(rig.ib._tick_req_ids[0], 101, _MSG_101, None)

    exc = await _wait_failed(handle)
    assert isinstance(exc, MarketDataLinesExhaustedError), repr(exc)
    assert exc.error_code == 101
    assert str(exc).startswith("[IB 101]")
    assert handle.closed is True
    assert collector.errors == [exc]
    collector.assert_data_has_no_exceptions()
    assert rig.transport.market_data_lines_in_use == 0
    assert _cancels(rig) == 1
    rig.transport.subscribe("trades", FakeContract(2), collector)  # line is reusable


async def test_closing_handle_after_error_101_is_a_no_op() -> None:
    rig = await _started(market_data_lines=2)
    collector = _Collector()
    failed = _open(rig, collector)
    survivor = rig.transport.subscribe("trades", FakeContract(2), _Collector())
    assert rig.transport.market_data_lines_in_use == 2

    rig.ib.errorEvent(rig.ib._tick_req_ids[0], 101, _MSG_101, None)
    await _wait_failed(failed)
    assert rig.transport.market_data_lines_in_use == 1

    failed.close()
    failed.close()

    assert rig.transport.market_data_lines_in_use == 1  # the survivor's line is still counted
    assert _cancels(rig) == 1  # cancelled once at failure, not again on close
    assert len(collector.errors) == 1  # on_error not called again
    survivor.close()
    assert rig.transport.market_data_lines_in_use == 0


async def test_error_101_is_delivered_once() -> None:
    rig = await _started(market_data_lines=1)
    collector = _Collector()
    handle = _open(rig, collector)
    req_id = rig.ib._tick_req_ids[0]

    rig.ib.errorEvent(req_id, 101, _MSG_101, None)
    await _wait_failed(handle)
    rig.ib.errorEvent(req_id, 101, _MSG_101, None)
    await asyncio.sleep(0.05)

    assert len(collector.errors) == 1
    assert _cancels(rig) == 1
    assert rig.transport.market_data_lines_in_use == 0


async def test_error_101_without_on_error_still_closes_handle_and_sets_error() -> None:
    rig = await _started(market_data_lines=1)
    collector = _Collector()
    handle = _open(rig, collector, with_on_error=False)

    rig.ib.errorEvent(rig.ib._tick_req_ids[0], 101, _MSG_101, None)

    exc = await _wait_failed(handle)
    assert isinstance(exc, MarketDataLinesExhaustedError), repr(exc)
    assert handle.closed is True
    collector.assert_data_has_no_exceptions()
    assert rig.transport.market_data_lines_in_use == 0
    handle.close()
    assert rig.transport.market_data_lines_in_use == 0


async def test_hard_permission_code_on_trades_reqid_frees_line_and_sets_error() -> None:
    rig = await _started(market_data_lines=1)
    collector = _Collector()
    handle = _open(rig, collector)

    rig.ib.errorEvent(rig.ib._tick_req_ids[0], 354, _MSG_354, None)

    exc = await _wait_failed(handle)
    assert isinstance(exc, MarketDataPermissionError), repr(exc)
    assert exc.error_code == 354
    assert handle.closed is True
    assert collector.errors == [exc]
    collector.assert_data_has_no_exceptions()
    assert rig.transport.market_data_lines_in_use == 0
    assert _cancels(rig) == 1
    handle.close()
    assert rig.transport.market_data_lines_in_use == 0
    assert _cancels(rig) == 1
    assert len(collector.errors) == 1


async def test_soft_permission_code_on_trades_reqid_keeps_stream_and_line() -> None:
    rig = await _started(market_data_lines=1)
    collector = _Collector()
    handle = _open(rig, collector)

    rig.ib.errorEvent(rig.ib._tick_req_ids[0], 2152, _MSG_2152, None)
    await asyncio.sleep(0.05)

    assert handle.error is None
    assert handle.closed is False
    assert collector.errors == []
    collector.assert_data_has_no_exceptions()
    assert rig.transport.market_data_lines_in_use == 1
    assert _cancels(rig) == 0
    handle.close()
    assert rig.transport.market_data_lines_in_use == 0


async def test_error_101_for_unknown_reqid_leaves_trades_stream_untouched() -> None:
    rig = await _started(market_data_lines=1)
    collector = _Collector()
    handle = _open(rig, collector)

    rig.ib.errorEvent(rig.ib._tick_req_ids[0] + 5000, 101, _MSG_101, None)
    await asyncio.sleep(0.05)

    assert handle.error is None
    assert handle.closed is False
    assert collector.errors == []
    assert rig.transport.market_data_lines_in_use == 1
    handle.close()


async def test_error_101_after_resubscribe_targets_the_new_request() -> None:
    rig = await _started(market_data_lines=1)
    collector = _Collector()
    handle = _open(rig, collector)
    rig.ib.rotate_tickers()
    rig.transport.resubscribe_all(cancel_old=False)
    assert len(rig.ib._tick_req_ids) == 2

    rig.ib.errorEvent(rig.ib._tick_req_ids[1], 101, _MSG_101, None)

    exc = await _wait_failed(handle)
    assert isinstance(exc, MarketDataLinesExhaustedError), repr(exc)
    assert handle.closed is True
    assert collector.errors == [exc]
    assert rig.transport.market_data_lines_in_use == 0
