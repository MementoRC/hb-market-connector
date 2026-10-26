"""Acceptance tests for issue #104: a per-transport cap on IB market-data lines.

Pinned decisions:
- ``IbConnectionSpec.market_data_lines`` (default 100) is the number of concurrent IB-side
  market-data streams one transport may hold. Each distinct L1 ticker (``top_of_book``), shared
  depth stream and tick-by-tick trades subscription counts one line; subscribers sharing an L1 or
  depth stream share its line, trades subscriptions are one IB request (one line) each.
- Opening a stream beyond the cap fails fast and synchronously (no waiting) with
  ``MarketDataLinesExhaustedError`` (a ``SubscriptionLimitError``) before any request reaches IB.
- Lines are freed when the stream is cancelled at IB: trades handle close, depth idle release,
  ``_release_all_streams`` / disconnect, a hard stream error.
- ``resubscribe_all`` re-issues streams on existing lines and never double-counts.
- IB error 101 on a stream's reqId fails the waiting call with the same exception and frees
  the line.
- Crossing 80% of the cap logs a warning, counted per transport.

The implementation must expose ``IbGatewayTransport.market_data_lines_in_use`` (int property).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from market_connector.exchanges.interactive_brokers.exceptions import (
    MarketDataLinesExhaustedError,
    SubscriptionLimitError,
)
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec

from .test_market_data_real_contract import _real_rig, _resolved
from .test_persistent_market_data import FakeContract, Rig, _rig, _set_l1

_PROMPT = 2.0
_IDLE_SHORT = 0.05
_IDLE_LONG = 5.0
_PAST_IDLE = 0.4
_MSG_101 = "Max number of tickers has been reached"


def _noop(_data: Any) -> None:
    return None


def _open_trades(rig: Rig, n: int, start: int = 1) -> list[Any]:
    return [rig.transport.subscribe("trades", FakeContract(start + i), _noop) for i in range(n)]


def _open_depth(rig: Rig, n: int, start: int = 1) -> list[Any]:
    return [rig.transport.subscribe("depth", FakeContract(start + i), _noop) for i in range(n)]


async def _open_l1(rig: Rig, n: int, start: int = 1) -> None:
    _set_l1(rig.ib._l1_ticker, 150.0, 150.5)
    for i in range(n):
        await rig.transport.top_of_book(FakeContract(start + i))


def _ib_calls(rig: Rig) -> tuple[int, int, int]:
    return (
        len(rig.ib.req_mkt_data_calls),
        len(rig.ib.req_mkt_depth_calls),
        len(rig.ib.req_tick_by_tick_calls),
    )


async def _started(**spec_kwargs: Any) -> Rig:
    spec_kwargs.setdefault("market_data_timeout", 30.0)
    rig = _real_rig(**spec_kwargs)
    await rig.gateway.start()
    return rig


async def _request_id(request_ids: list[int]) -> int:
    for _ in range(500):
        await asyncio.sleep(0.002)
        if request_ids:
            return request_ids[0]
    raise AssertionError("connector never sent the market data request")


# --------------------------------------------------------------------------- AC1


def test_ac1_market_data_lines_defaults_to_100() -> None:
    assert IbConnectionSpec().market_data_lines == 100


def test_ac1_market_data_lines_accepts_positive_value() -> None:
    assert IbConnectionSpec(market_data_lines=7).market_data_lines == 7


@pytest.mark.parametrize("value", [0, -1, -100])
def test_ac1_non_positive_market_data_lines_rejected(value: int) -> None:
    with pytest.raises(ValueError, match="market_data_lines"):
        IbConnectionSpec(market_data_lines=value)


def test_exhausted_error_is_a_subscription_limit_error() -> None:
    assert issubclass(MarketDataLinesExhaustedError, SubscriptionLimitError)


# --------------------------------------------------------------------------- AC2 / AC3


async def test_ac2_each_stream_kind_counts_one_line() -> None:
    rig = _rig(market_data_lines=10)
    assert rig.transport.market_data_lines_in_use == 0
    await _open_l1(rig, 1)
    assert rig.transport.market_data_lines_in_use == 1
    _open_depth(rig, 1, start=2)
    assert rig.transport.market_data_lines_in_use == 2
    _open_trades(rig, 1, start=3)
    assert rig.transport.market_data_lines_in_use == 3


async def test_ac2_counts_are_per_transport() -> None:
    first = _rig(market_data_lines=2)
    second = _rig(market_data_lines=2)
    _open_depth(first, 2)
    assert first.transport.market_data_lines_in_use == 2
    assert second.transport.market_data_lines_in_use == 0
    _open_depth(second, 2)  # a shared count would exhaust here
    assert second.transport.market_data_lines_in_use == 2


async def test_ac3_depth_subscribers_share_one_line() -> None:
    rig = _rig(market_data_lines=1)
    contract = FakeContract(1)
    handles = [rig.transport.subscribe("depth", contract, _noop) for _ in range(3)]
    assert rig.transport.market_data_lines_in_use == 1
    assert len(rig.ib.req_mkt_depth_calls) == 1
    for handle in handles:
        handle.close()


async def test_ac3_l1_callers_share_one_line() -> None:
    rig = _rig(market_data_lines=1)
    _set_l1(rig.ib._l1_ticker, 150.0, 150.5)
    contract = FakeContract(1)
    await rig.transport.top_of_book(contract)
    await rig.transport.top_of_book(contract)
    assert rig.transport.market_data_lines_in_use == 1
    assert len(rig.ib.req_mkt_data_calls) == 1


async def test_ac3_depth_snapshot_shares_line_with_subscriber() -> None:
    rig = _rig(market_data_lines=1, market_data_timeout=0.1)
    contract = FakeContract(1)
    handle = rig.transport.subscribe("depth", contract, _noop)
    with pytest.raises(Exception) as info:  # noqa: PT011 - timeout (no depth fed) is expected
        await rig.transport.depth_snapshot(contract)
    assert not isinstance(info.value, MarketDataLinesExhaustedError)
    assert len(rig.ib.req_mkt_depth_calls) == 1
    handle.close()


# --------------------------------------------------------------------------- AC4


async def test_ac4_trades_beyond_cap_raises_without_ib_request() -> None:
    rig = _rig(market_data_lines=2)
    _open_trades(rig, 2)
    before = _ib_calls(rig)
    with pytest.raises(MarketDataLinesExhaustedError):
        rig.transport.subscribe("trades", FakeContract(99), _noop)
    assert _ib_calls(rig) == before


async def test_ac4_depth_beyond_cap_raises_without_ib_request() -> None:
    rig = _rig(market_data_lines=2)
    _open_depth(rig, 2)
    before = _ib_calls(rig)
    with pytest.raises(MarketDataLinesExhaustedError):
        rig.transport.subscribe("depth", FakeContract(99), _noop)
    assert _ib_calls(rig) == before


async def test_ac4_depth_snapshot_beyond_cap_raises_without_ib_request() -> None:
    rig = _rig(market_data_lines=1)
    _open_depth(rig, 1)
    before = _ib_calls(rig)
    with pytest.raises(MarketDataLinesExhaustedError):
        await asyncio.wait_for(rig.transport.depth_snapshot(FakeContract(99)), _PROMPT)
    assert _ib_calls(rig) == before


async def test_ac4_l1_beyond_cap_raises_without_ib_request() -> None:
    rig = _rig(market_data_lines=2)
    await _open_l1(rig, 2)
    before = _ib_calls(rig)
    with pytest.raises(MarketDataLinesExhaustedError):
        await asyncio.wait_for(rig.transport.top_of_book(FakeContract(99)), _PROMPT)
    assert _ib_calls(rig) == before


async def test_ac4_cap_is_shared_across_stream_kinds() -> None:
    rig = _rig(market_data_lines=3)
    await _open_l1(rig, 1)
    _open_depth(rig, 1, start=2)
    _open_trades(rig, 1, start=3)
    before = _ib_calls(rig)
    with pytest.raises(MarketDataLinesExhaustedError):
        rig.transport.subscribe("trades", FakeContract(98), _noop)
    with pytest.raises(MarketDataLinesExhaustedError):
        rig.transport.subscribe("depth", FakeContract(99), _noop)
    assert _ib_calls(rig) == before
    assert rig.transport.market_data_lines_in_use == 3


async def test_ac4_exhaustion_is_caught_as_subscription_limit_error() -> None:
    rig = _rig(market_data_lines=1)
    _open_trades(rig, 1)
    with pytest.raises(SubscriptionLimitError):
        rig.transport.subscribe("trades", FakeContract(99), _noop)


async def test_ac4_existing_stream_at_cap_is_still_reusable() -> None:
    rig = _rig(market_data_lines=1)
    _open_depth(rig, 1)
    _open_depth(rig, 1)  # same contract: shares the one line, no error
    assert rig.transport.market_data_lines_in_use == 1


# --------------------------------------------------------------------------- AC5


async def test_ac5_closing_trades_handle_frees_line() -> None:
    rig = _rig(market_data_lines=1)
    (handle,) = _open_trades(rig, 1)
    with pytest.raises(MarketDataLinesExhaustedError):
        rig.transport.subscribe("trades", FakeContract(2), _noop)
    handle.close()
    assert rig.transport.market_data_lines_in_use == 0
    rig.transport.subscribe("trades", FakeContract(2), _noop)
    assert len(rig.ib.req_tick_by_tick_calls) == 2


async def test_ac5_depth_line_held_during_idle_window() -> None:
    rig = _rig(market_data_lines=1, depth_idle_release=_IDLE_LONG)
    (handle,) = _open_depth(rig, 1)
    handle.close()
    with pytest.raises(MarketDataLinesExhaustedError):
        rig.transport.subscribe("depth", FakeContract(2), _noop)


async def test_ac5_depth_line_freed_after_idle_timeout() -> None:
    rig = _rig(market_data_lines=1, depth_idle_release=_IDLE_SHORT)
    (handle,) = _open_depth(rig, 1)
    handle.close()
    await asyncio.sleep(_PAST_IDLE)
    assert rig.transport.market_data_lines_in_use == 0
    rig.transport.subscribe("depth", FakeContract(2), _noop)
    assert len(rig.ib.req_mkt_depth_calls) == 2


async def test_ac5_depth_line_kept_while_any_subscriber_remains() -> None:
    rig = _rig(market_data_lines=1, depth_idle_release=_IDLE_SHORT)
    contract = FakeContract(1)
    first = rig.transport.subscribe("depth", contract, _noop)
    second = rig.transport.subscribe("depth", contract, _noop)
    first.close()
    await asyncio.sleep(_PAST_IDLE)
    assert rig.transport.market_data_lines_in_use == 1
    second.close()


async def test_ac5_release_all_streams_frees_every_line_including_trades() -> None:
    rig = _rig(market_data_lines=3)
    await _open_l1(rig, 1)
    _open_depth(rig, 1, start=2)
    _open_trades(rig, 1, start=3)
    rig.transport._release_all_streams()
    assert rig.transport.market_data_lines_in_use == 0
    rig.transport.subscribe("trades", FakeContract(4), _noop)
    rig.transport.subscribe("trades", FakeContract(5), _noop)
    rig.transport.subscribe("trades", FakeContract(6), _noop)


async def test_ac5_disconnect_frees_lines() -> None:
    rig = await _started(market_data_lines=2)
    _open_trades(rig, 1)
    _open_depth(rig, 1, start=2)
    await rig.transport.disconnect()
    assert rig.transport.market_data_lines_in_use == 0


async def test_ac5_stream_dropped_after_hard_error_frees_line() -> None:
    rig = await _started(market_data_lines=1)
    task = asyncio.ensure_future(rig.transport.top_of_book(_resolved()))
    try:
        rig.ib.errorEvent(
            await _request_id(rig.ib._mkt_data_req_ids),
            354,
            "Requested market data is not subscribed.",
            None,
        )
        await asyncio.wait({task}, timeout=_PROMPT)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert rig.transport.market_data_lines_in_use == 0
    rig.transport.subscribe("depth", FakeContract(2), _noop)


# --------------------------------------------------------------------------- AC6


async def test_ac6_resubscribe_all_reissues_streams_without_double_counting() -> None:
    rig = _rig(market_data_lines=3)
    await _open_l1(rig, 1)
    _open_depth(rig, 1, start=2)
    _open_trades(rig, 1, start=3)
    before = rig.transport.market_data_lines_in_use
    rig.transport.resubscribe_all(cancel_old=False)
    assert rig.transport.market_data_lines_in_use == before == 3
    # each stream was really re-requested, not silently refused by the cap
    assert _ib_calls(rig) == (2, 2, 2)


async def test_ac6_reconnect_at_cap_still_blocks_next_stream() -> None:
    rig = _rig(market_data_lines=3)
    await _open_l1(rig, 1)
    _open_depth(rig, 1, start=2)
    _open_trades(rig, 1, start=3)
    rig.transport.resubscribe_all(cancel_old=True)
    assert _ib_calls(rig) == (2, 2, 2)
    after_reconnect = _ib_calls(rig)
    with pytest.raises(MarketDataLinesExhaustedError):
        rig.transport.subscribe("trades", FakeContract(99), _noop)
    assert _ib_calls(rig) == after_reconnect


async def test_ac6_repeated_resubscribe_keeps_usage_stable() -> None:
    rig = _rig(market_data_lines=2)
    _open_depth(rig, 1)
    _open_trades(rig, 1, start=2)
    for _ in range(3):
        rig.transport.resubscribe_all(cancel_old=False)
    assert rig.transport.market_data_lines_in_use == 2


# --------------------------------------------------------------------------- AC7


async def test_ac7_error_101_fails_top_of_book_and_frees_line() -> None:
    rig = await _started(market_data_lines=1)
    task = asyncio.ensure_future(rig.transport.top_of_book(_resolved()))
    try:
        rig.ib.errorEvent(await _request_id(rig.ib._mkt_data_req_ids), 101, _MSG_101, None)
        done, _ = await asyncio.wait({task}, timeout=_PROMPT)
        assert done, "error 101 did not reach the waiting top_of_book call"
        exc = task.exception()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert isinstance(exc, MarketDataLinesExhaustedError), repr(exc)
    assert exc.error_code == 101
    assert str(exc).startswith("[IB 101]")
    assert rig.transport.market_data_lines_in_use == 0
    rig.transport.subscribe("depth", FakeContract(2), _noop)


async def test_ac7_error_101_fails_depth_snapshot_and_frees_line() -> None:
    rig = await _started(market_data_lines=1)
    task = asyncio.ensure_future(rig.transport.depth_snapshot(_resolved()))
    try:
        rig.ib.errorEvent(await _request_id(rig.ib._depth_req_ids), 101, _MSG_101, None)
        done, _ = await asyncio.wait({task}, timeout=_PROMPT)
        assert done, "error 101 did not reach the waiting depth_snapshot call"
        exc = task.exception()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert isinstance(exc, MarketDataLinesExhaustedError), repr(exc)
    assert exc.error_code == 101
    assert rig.transport.market_data_lines_in_use == 0
    rig.transport.subscribe("trades", FakeContract(2), _noop)


# --------------------------------------------------------------------------- AC8


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_ac8_warns_when_usage_crosses_80_percent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = _rig(market_data_lines=10)
    with caplog.at_level(logging.WARNING):
        _open_trades(rig, 7)
        assert _warnings(caplog) == []
        _open_trades(rig, 1, start=100)  # 8 of 10 = 80%
        assert len(_warnings(caplog)) >= 1


async def test_ac8_warning_is_per_transport_not_process_global(
    caplog: pytest.LogCaptureFixture,
) -> None:
    first = _rig(market_data_lines=10)
    second = _rig(market_data_lines=10)
    _open_trades(first, 5)
    with caplog.at_level(logging.WARNING):
        _open_trades(second, 5)  # 10 lines process-wide, but only 5 of 10 here
        assert _warnings(caplog) == []
        _open_trades(second, 3, start=100)  # 8 of 10 on this transport
        assert len(_warnings(caplog)) >= 1
