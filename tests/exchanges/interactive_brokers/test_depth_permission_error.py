"""Acceptance tests for issue #75: permission errors must reach the waiting market-data request.

IB reports "Need additional market data permissions" (2152) and "not subscribed" (354) against
the reqId of a market-data request. Today the router only fails waiters registered in
``_pending_request_waiters``; the persistent depth and L1 streams register none, so the error is
dropped and the caller waits out ``market_data_timeout`` and gets MarketDataTimeoutError.

Pinned expectations (these tests are RED until the fix lands):
- The waiting depth / L1 call raises MarketDataPermissionError promptly, not after the timeout.
- The message keeps IB's text and the ``[IB <code>]`` prefix; ``error_code`` carries the code.
- The released depth stream is cancelled at IB and forgotten by the transport.
- Connection state is untouched and unrelated waiters keep waiting.
- A permission error with reqId -1 (not tied to a request) is harmless.

The fake IB does not assign request ids, so ``_number_requests`` gives each reqMktDepth /
reqMktData call a request id; tests inject errors against that id, as IB would.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from market_connector.exchanges.interactive_brokers.exceptions import MarketDataPermissionError

from .test_market_data_real_contract import _real_rig, _resolved
from .test_persistent_market_data import (
    _EXPECTED_ASKS,
    _EXPECTED_BIDS,
    Rig,
    _feed_levels,
)

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from ._doubles import MockIb

_MARKET_DATA_TIMEOUT = 30.0  # far beyond the bound below: a timeout-based failure cannot pass
_PROMPT = 2.0  # seconds a permission error must take to surface

_MSG_2152 = (
    "Exchanges - Top: IBEOS; OVERNIGHT; Need additional market data permissions - "
    "Depth: NASDAQ; BATS; ARCA"
)
_MSG_354 = "Requested market data is not subscribed."


def _number_requests(ib: MockIb) -> dict[str, list[int]]:
    """Give each reqMktDepth / reqMktData call the request id the connector sent to IB."""
    ids: dict[str, list[int]] = {"depth": [], "l1": []}
    next_id = iter(range(1001, 100_000))
    send_depth, send_l1 = ib.reqMktDepth, ib.reqMktData

    def req_depth(contract: Any, numRows: int = 10, isSmartDepth: bool = True) -> Any:  # noqa: N803
        ids["depth"].append(next(next_id))
        return send_depth(contract, numRows=numRows, isSmartDepth=isSmartDepth)

    def req_l1(contract: Any, *args: Any, **kwargs: Any) -> Any:
        ids["l1"].append(next(next_id))
        return send_l1(contract, *args, **kwargs)

    ib.reqMktDepth = req_depth
    ib.reqMktData = req_l1
    return ids


async def _started_rig() -> tuple[Rig, dict[str, list[int]]]:
    """Connected gateway (router attached to errorEvent) over the fake IB, requests numbered."""
    rig = _real_rig(market_data_timeout=_MARKET_DATA_TIMEOUT)
    await rig.gateway.start()
    return rig, _number_requests(rig.ib)


async def _request_id(request_ids: list[int]) -> int:
    """Wait until the connector has sent a request; return its id."""
    for _ in range(500):
        await asyncio.sleep(0.002)
        if request_ids:
            return request_ids[0]
    raise AssertionError("connector never sent the market data request")


async def _fails_on_request(
    rig: Rig,
    call: Coroutine[Any, Any, Any],
    request_ids: list[int],
    code: int,
    msg: str,
) -> MarketDataPermissionError:
    """Run *call*, inject IB error *code* against its request id; return the prompt failure."""
    task = asyncio.ensure_future(call)
    try:
        rig.ib.errorEvent(await _request_id(request_ids), code, msg, None)
        with pytest.raises(MarketDataPermissionError) as info:
            await asyncio.wait_for(task, _PROMPT)
        return info.value
    finally:
        await _cancel(task)


async def _cancel(*tasks: asyncio.Future[Any]) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


# --------------------------------------------------------------------------- depth


@pytest.mark.parametrize(("code", "msg"), [(2152, _MSG_2152), (354, _MSG_354)])
async def test_depth_snapshot_raises_permission_error_promptly(code: int, msg: str) -> None:
    rig, ids = await _started_rig()
    resolved = _resolved()
    await _fails_on_request(rig, rig.transport.depth_snapshot(resolved), ids["depth"], code, msg)


async def test_get_orderbook_raises_permission_error_promptly() -> None:
    rig, ids = await _started_rig()
    await _fails_on_request(
        rig, rig.gateway.get_orderbook("AAPL-USD"), ids["depth"], 2152, _MSG_2152
    )


async def test_permission_error_message_keeps_ib_text_and_code() -> None:
    rig, ids = await _started_rig()
    exc = await _fails_on_request(
        rig, rig.transport.depth_snapshot(_resolved()), ids["depth"], 2152, _MSG_2152
    )
    assert exc.error_code == 2152
    assert "Need additional market data permissions" in str(exc)
    assert str(exc).startswith("[IB 2152]")


async def test_depth_stream_is_cancelled_and_forgotten_after_permission_error() -> None:
    rig, ids = await _started_rig()
    resolved = _resolved()
    await _fails_on_request(
        rig, rig.transport.depth_snapshot(resolved), ids["depth"], 2152, _MSG_2152
    )
    assert len(rig.ib._cancel_mkt_depth_calls) == 1
    assert rig.ib._cancel_mkt_depth_calls[0] is resolved.native
    assert rig.transport._depth_streams == {}


# ------------------------------------------------------------------------------- L1


@pytest.mark.parametrize(("code", "msg"), [(354, _MSG_354), (2152, _MSG_2152)])
async def test_l1_mid_price_raises_permission_error_promptly(code: int, msg: str) -> None:
    rig, ids = await _started_rig()
    exc = await _fails_on_request(rig, rig.gateway.get_mid_price("AAPL-USD"), ids["l1"], code, msg)
    assert exc.error_code == code


# ------------------------------------------------------- connection and other waiters


async def test_permission_error_leaves_connection_and_other_waiters_alone() -> None:
    rig, ids = await _started_rig()
    events: list[object] = []
    handle = rig.transport.subscribe_connection_state(events.append)
    other = asyncio.ensure_future(rig.transport.top_of_book(_resolved(272093, "MSFT")))
    try:
        await _fails_on_request(
            rig, rig.transport.depth_snapshot(_resolved()), ids["depth"], 2152, _MSG_2152
        )
        assert rig.transport.is_connected
        assert events == []
        assert not other.done()
    finally:
        handle.close()
        await _cancel(other)


async def test_permission_error_without_request_id_is_harmless() -> None:
    rig, ids = await _started_rig()
    events: list[object] = []
    handle = rig.transport.subscribe_connection_state(events.append)
    snapshot = asyncio.ensure_future(rig.transport.depth_snapshot(_resolved()))
    try:
        await _request_id(ids["depth"])
        rig.ib.errorEvent(-1, 2152, _MSG_2152, None)
        _feed_levels(rig.ib._depth_ticker)
        bids, asks = await asyncio.wait_for(snapshot, _PROMPT)
    finally:
        handle.close()
        await _cancel(snapshot)
    assert (bids, asks) == (_EXPECTED_BIDS, _EXPECTED_ASKS)
    assert rig.transport.is_connected
    assert events == []
