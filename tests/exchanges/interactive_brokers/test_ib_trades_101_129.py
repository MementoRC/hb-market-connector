"""Acceptance tests for issue #129: IB error 101 (and hard permission codes) on trades streams.

``subscribe("trades")`` issues ``reqTickByTickData`` and counts one market-data line. Today the
reqId of that request is not tracked, so an IB error on it is only logged: the subscriber never
learns of it and the line stays counted until the handle closes.

Pinned behaviour (mirrors the L1/depth path of issue #104, ``test_market_data_line_cap_104``):
- IB error 101 on a trades stream's reqId delivers ``MarketDataLinesExhaustedError`` to the
  subscriber and releases the line (the IB stream is cancelled once).
- A hard permission code (354) does the same with ``MarketDataPermissionError``.
- Soft permission codes and unknown reqIds leave the stream and its line untouched.
- Closing the handle after such a failure does not release the line a second time.

Subscriber-error mechanism: ``subscribe`` has no error channel today, so the typed exception
instance is delivered through the subscriber's own callback (the only subscriber-facing path of
``IbGatewayTransport.subscribe``), as the one argument it normally gets a tick in.
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
    """Subscriber callback recording everything delivered to it."""

    def __init__(self) -> None:
        self.items: list[Any] = []

    def __call__(self, data: Any) -> None:
        self.items.append(data)

    @property
    def errors(self) -> list[BaseException]:
        return [i for i in self.items if isinstance(i, BaseException)]


async def _open(rig: Rig, collector: _Collector, con_id: int = 1) -> Any:
    handle = rig.transport.subscribe("trades", _resolved(con_id), collector)
    assert rig.ib._tick_req_ids, "FakeIB did not assign a reqId to reqTickByTickData"
    return handle


async def _wait_error(collector: _Collector) -> BaseException:
    for _ in range(int(_PROMPT / 0.005)):
        if collector.errors:
            return collector.errors[0]
        await asyncio.sleep(0.005)
    raise AssertionError("the subscriber never observed the error")


def _cancels(rig: Rig) -> int:
    return len(rig.ib._cancel_tick_by_tick_calls)


async def test_error_101_on_trades_reqid_frees_line_and_subscriber_sees_typed_error() -> None:
    rig = await _started(market_data_lines=1)
    collector = _Collector()
    await _open(rig, collector)
    assert rig.transport.market_data_lines_in_use == 1

    rig.ib.errorEvent(rig.ib._tick_req_ids[0], 101, _MSG_101, None)

    exc = await _wait_error(collector)
    assert isinstance(exc, MarketDataLinesExhaustedError), repr(exc)
    assert exc.error_code == 101
    assert str(exc).startswith("[IB 101]")
    assert rig.transport.market_data_lines_in_use == 0
    assert _cancels(rig) == 1
    rig.transport.subscribe("trades", FakeContract(2), collector)  # line is reusable


async def test_closing_handle_after_error_101_does_not_release_line_twice() -> None:
    rig = await _started(market_data_lines=2)
    collector = _Collector()
    failed = await _open(rig, collector)
    survivor = rig.transport.subscribe("trades", FakeContract(2), _Collector())
    assert rig.transport.market_data_lines_in_use == 2

    rig.ib.errorEvent(rig.ib._tick_req_ids[0], 101, _MSG_101, None)
    await _wait_error(collector)
    assert rig.transport.market_data_lines_in_use == 1

    failed.close()
    failed.close()

    assert rig.transport.market_data_lines_in_use == 1  # the survivor's line is still counted
    assert _cancels(rig) == 1  # the failed stream was cancelled once, not again on close
    survivor.close()
    assert rig.transport.market_data_lines_in_use == 0


async def test_error_101_is_delivered_once() -> None:
    rig = await _started(market_data_lines=1)
    collector = _Collector()
    await _open(rig, collector)
    req_id = rig.ib._tick_req_ids[0]

    rig.ib.errorEvent(req_id, 101, _MSG_101, None)
    await _wait_error(collector)
    rig.ib.errorEvent(req_id, 101, _MSG_101, None)
    await asyncio.sleep(0.05)

    assert len(collector.errors) == 1


async def test_hard_permission_code_on_trades_reqid_frees_line_and_subscriber_sees_error() -> None:
    rig = await _started(market_data_lines=1)
    collector = _Collector()
    handle = await _open(rig, collector)

    rig.ib.errorEvent(rig.ib._tick_req_ids[0], 354, _MSG_354, None)

    exc = await _wait_error(collector)
    assert isinstance(exc, MarketDataPermissionError), repr(exc)
    assert exc.error_code == 354
    assert rig.transport.market_data_lines_in_use == 0
    assert _cancels(rig) == 1
    handle.close()
    assert rig.transport.market_data_lines_in_use == 0
    assert _cancels(rig) == 1


async def test_soft_permission_code_on_trades_reqid_keeps_stream_and_line() -> None:
    rig = await _started(market_data_lines=1)
    collector = _Collector()
    handle = await _open(rig, collector)

    rig.ib.errorEvent(rig.ib._tick_req_ids[0], 2152, _MSG_2152, None)
    await asyncio.sleep(0.05)

    assert collector.errors == []
    assert rig.transport.market_data_lines_in_use == 1
    assert _cancels(rig) == 0
    handle.close()
    assert rig.transport.market_data_lines_in_use == 0


async def test_error_101_for_unknown_reqid_leaves_trades_stream_untouched() -> None:
    rig = await _started(market_data_lines=1)
    collector = _Collector()
    handle = await _open(rig, collector)

    rig.ib.errorEvent(rig.ib._tick_req_ids[0] + 5000, 101, _MSG_101, None)
    await asyncio.sleep(0.05)

    assert collector.errors == []
    assert rig.transport.market_data_lines_in_use == 1
    handle.close()


async def test_error_101_after_resubscribe_targets_the_new_request() -> None:
    rig = await _started(market_data_lines=1)
    collector = _Collector()
    await _open(rig, collector)
    rig.ib.rotate_tickers()
    rig.transport.resubscribe_all(cancel_old=False)
    assert len(rig.ib._tick_req_ids) == 2

    rig.ib.errorEvent(rig.ib._tick_req_ids[1], 101, _MSG_101, None)

    exc = await _wait_error(collector)
    assert isinstance(exc, MarketDataLinesExhaustedError), repr(exc)
    assert rig.transport.market_data_lines_in_use == 0
