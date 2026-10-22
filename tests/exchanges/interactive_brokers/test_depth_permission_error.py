"""Acceptance tests for issue #75: IB permission errors must reach the waiting market-data request.

IB reports market-data permission problems against the reqId of a market-data request. The
codes split into two groups, and the tests pin the difference:

- HARD (354 "not subscribed"): the request can never produce data. The waiting depth / L1
  call raises MarketDataPermissionError promptly, the released depth stream is cancelled at
  IB and forgotten by the transport.
- SOFT (2152 / 10089 / 10090 for depth, 10167 for L1): IB still delivers what it can, so a
  waiter keeps waiting. It only fails, with MarketDataPermissionError, if no data arrives
  before ``market_data_timeout``.

Also pinned: the message keeps IB's text and the ``[IB <code>]`` prefix, the connection is
untouched, unrelated waiters keep waiting, and a permission error with reqId -1 is harmless.

The fake IB assigns ib_async-style request ids (``_depth_req_ids`` / ``_mkt_data_req_ids``);
tests inject IB errors against those ids, as IB would.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest

from market_connector.exchanges.interactive_brokers.exceptions import MarketDataPermissionError

from .test_market_data_real_contract import _real_rig, _resolved
from .test_persistent_market_data import (
    _EXPECTED_ASKS,
    _EXPECTED_BIDS,
    Rig,
    _feed_levels,
    _fire,
    _set_l1,
)

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from ._doubles import FakeTicker

_MARKET_DATA_TIMEOUT = 30.0  # far beyond the bounds below: a timeout-based failure cannot pass
_PROMPT = 2.0  # seconds within which a permission error must surface
_SHORT_TIMEOUT = 0.3  # market_data_timeout for the no-data soft-code test

_MSG_2152 = (
    "Exchanges - Top: IBEOS; OVERNIGHT; Need additional market data permissions - "
    "Depth: NASDAQ; BATS; ARCA"
)
_MSG_354 = "Requested market data is not subscribed."
_MSG_10089 = "Requested market data requires additional subscription for API."
_MSG_10090 = "Part of requested market data is not subscribed. Other ticks are still active."
_MSG_10167 = "Requested market data is not subscribed. Displaying delayed market data."

_SOFT_DEPTH = [(2152, _MSG_2152), (10089, _MSG_10089), (10090, _MSG_10090)]


async def _started_rig(**spec_kwargs: Any) -> Rig:
    """Connected gateway (router attached to errorEvent) over the fake IB."""
    spec_kwargs.setdefault("market_data_timeout", _MARKET_DATA_TIMEOUT)
    rig = _real_rig(**spec_kwargs)
    await rig.gateway.start()
    return rig


async def _request_id(request_ids: list[int]) -> int:
    """Wait until the connector has sent a request; return its id."""
    for _ in range(500):
        await asyncio.sleep(0.002)
        if request_ids:
            return request_ids[0]
    raise AssertionError("connector never sent the market data request")


async def _exception_within(task: asyncio.Future[Any], seconds: float) -> BaseException:
    """Return the error *task* fails with; fail if it has none or is not done in time."""
    done, _ = await asyncio.wait({task}, timeout=seconds)
    if not done:
        pytest.fail(f"no outcome within {seconds}s: the error did not reach the waiter")
    exc = task.exception()
    if exc is None:
        pytest.fail(f"expected an error, got result {task.result()!r}")
    return exc


async def _hard_error(
    rig: Rig, call: Coroutine[Any, Any, Any], request_ids: list[int], code: int, msg: str
) -> MarketDataPermissionError:
    """Run *call*, inject IB error *code* against its request id; return the prompt failure."""
    task = asyncio.ensure_future(call)
    try:
        rig.ib.errorEvent(await _request_id(request_ids), code, msg, None)
        exc = await _exception_within(task, _PROMPT)
    finally:
        await _cancel(task)
    assert isinstance(exc, MarketDataPermissionError), repr(exc)
    return exc


async def _cancel(*tasks: asyncio.Future[Any]) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def _quote_later(ticker: FakeTicker, bid: float, ask: float, delay: float = 0.01) -> None:
    """Deliver an L1 quote update shortly after the stream under test has been opened."""

    def deliver() -> None:
        _set_l1(ticker, bid, ask)
        _fire(ticker)

    asyncio.get_running_loop().call_later(delay, deliver)


def _assert_depth_released(rig: Rig, resolved: Any) -> None:
    assert len(rig.ib._cancel_mkt_depth_calls) == 1
    assert rig.ib._cancel_mkt_depth_calls[0] is resolved.native
    assert rig.transport._depth_streams == {}


# --------------------------------------------------------------------------- hard 354: depth


async def test_hard_354_depth_snapshot_raises_permission_error_promptly() -> None:
    rig = await _started_rig()
    await _hard_error(
        rig, rig.transport.depth_snapshot(_resolved()), rig.ib._depth_req_ids, 354, _MSG_354
    )


async def test_hard_354_get_orderbook_raises_permission_error_promptly() -> None:
    rig = await _started_rig()
    await _hard_error(
        rig, rig.gateway.get_orderbook("AAPL-USD"), rig.ib._depth_req_ids, 354, _MSG_354
    )


async def test_hard_354_message_keeps_ib_text_and_code() -> None:
    rig = await _started_rig()
    exc = await _hard_error(
        rig, rig.transport.depth_snapshot(_resolved()), rig.ib._depth_req_ids, 354, _MSG_354
    )
    assert exc.error_code == 354
    assert str(exc).startswith("[IB 354]")
    assert _MSG_354 in str(exc)


async def test_hard_354_depth_stream_is_cancelled_and_forgotten() -> None:
    rig = await _started_rig()
    resolved = _resolved()
    await _hard_error(
        rig, rig.transport.depth_snapshot(resolved), rig.ib._depth_req_ids, 354, _MSG_354
    )
    _assert_depth_released(rig, resolved)


async def test_hard_354_on_depth_leaves_connection_and_other_waiters_alone() -> None:
    rig = await _started_rig()
    events: list[object] = []
    handle = rig.transport.subscribe_connection_state(events.append)
    other = asyncio.ensure_future(rig.transport.top_of_book(_resolved(272093, "MSFT")))
    try:
        await _hard_error(
            rig, rig.transport.depth_snapshot(_resolved()), rig.ib._depth_req_ids, 354, _MSG_354
        )
        assert rig.transport.is_connected
        assert events == []
        assert not other.done()
    finally:
        handle.close()
        await _cancel(other)


# ------------------------------------------------------------------- soft codes: depth


@pytest.mark.parametrize(("code", "msg"), _SOFT_DEPTH)
async def test_soft_depth_code_returns_snapshot_when_levels_arrive(code: int, msg: str) -> None:
    rig = await _started_rig()
    task = asyncio.ensure_future(rig.transport.depth_snapshot(_resolved()))
    try:
        rig.ib.errorEvent(await _request_id(rig.ib._depth_req_ids), code, msg, None)
        _feed_levels(rig.ib._depth_ticker)
        assert await asyncio.wait_for(task, _PROMPT) == (_EXPECTED_BIDS, _EXPECTED_ASKS)
    finally:
        await _cancel(task)


async def test_soft_2152_without_data_raises_permission_error_at_timeout() -> None:
    rig = await _started_rig(market_data_timeout=_SHORT_TIMEOUT)
    resolved = _resolved()
    task = asyncio.ensure_future(rig.transport.depth_snapshot(resolved))
    try:
        rig.ib.errorEvent(await _request_id(rig.ib._depth_req_ids), 2152, _MSG_2152, None)
        exc = await _exception_within(task, _PROMPT)
    finally:
        await _cancel(task)
    assert isinstance(exc, MarketDataPermissionError), repr(exc)
    assert exc.error_code == 2152
    assert str(exc).startswith("[IB 2152]")
    assert "Need additional market data permissions" in str(exc)
    _assert_depth_released(rig, resolved)


async def test_soft_timeout_does_not_drop_depth_stream_shared_with_another_holder() -> None:
    rig = await _started_rig(market_data_timeout=_SHORT_TIMEOUT)
    resolved = _resolved()
    # A long-lived depth subscriber holds the contract's shared stream before the snapshot.
    holder = rig.transport.subscribe("depth", resolved, lambda _book: None)
    task = asyncio.ensure_future(rig.transport.depth_snapshot(resolved))
    try:
        rig.ib.errorEvent(await _request_id(rig.ib._depth_req_ids), 2152, _MSG_2152, None)
        exc = await _exception_within(task, _PROMPT)
        assert isinstance(exc, MarketDataPermissionError), repr(exc)
        assert exc.error_code == 2152
        assert rig.ib._cancel_mkt_depth_calls == []
        (surviving,) = rig.transport._depth_streams.values()
        _feed_levels(rig.ib._depth_ticker)
        assert list(surviving.bids) == _EXPECTED_BIDS
        assert list(surviving.asks) == _EXPECTED_ASKS
    finally:
        await _cancel(task)
        holder.close()


# ------------------------------------------------------------------------ L1 mid price


async def test_hard_354_on_l1_mid_price_raises_permission_error_promptly() -> None:
    rig = await _started_rig()
    exc = await _hard_error(
        rig, rig.gateway.get_mid_price("AAPL-USD"), rig.ib._mkt_data_req_ids, 354, _MSG_354
    )
    assert exc.error_code == 354


async def test_soft_10167_on_l1_still_returns_mid_price_when_quotes_arrive() -> None:
    rig = await _started_rig()
    task = asyncio.ensure_future(rig.gateway.get_mid_price("AAPL-USD"))
    try:
        rig.ib.errorEvent(await _request_id(rig.ib._mkt_data_req_ids), 10167, _MSG_10167, None)
        _quote_later(rig.ib._l1_ticker, 150.0, 150.5, delay=0.0)
        assert await asyncio.wait_for(task, _PROMPT) == Decimal("150.25")
    finally:
        await _cancel(task)


# ------------------------------------------------------- reqId -1 is not tied to a request


@pytest.mark.parametrize(("code", "msg"), [(2152, _MSG_2152), (354, _MSG_354)])
async def test_permission_error_without_request_id_is_harmless(code: int, msg: str) -> None:
    rig = await _started_rig()
    events: list[object] = []
    handle = rig.transport.subscribe_connection_state(events.append)
    snapshot = asyncio.ensure_future(rig.transport.depth_snapshot(_resolved()))
    try:
        await _request_id(rig.ib._depth_req_ids)
        rig.ib.errorEvent(-1, code, msg, None)
        _feed_levels(rig.ib._depth_ticker)
        bids, asks = await asyncio.wait_for(snapshot, _PROMPT)
    finally:
        handle.close()
        await _cancel(snapshot)
    assert (bids, asks) == (_EXPECTED_BIDS, _EXPECTED_ASKS)
    assert rig.transport.is_connected
    assert events == []
