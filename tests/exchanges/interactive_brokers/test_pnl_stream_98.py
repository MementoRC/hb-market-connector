"""Acceptance tests for issue #98: streaming PnL subscriptions.

Red phase: ``subscribe_pnl`` / ``subscribe_position_pnl`` do not exist yet, so they are
reached via ``# type: ignore[attr-defined]`` and each test fails on its own.

Contract pinned here:
- Transport ``subscribe_pnl(cb)`` / ``subscribe_position_pnl(con_id, cb)`` are synchronous,
  raise ConnectionLostError when not connected, and return a SubscriptionHandle.
- Exactly one live IB request per key (ib_async asserts on duplicates); subscribers and
  snapshots on the same key share it. The last release cancels it.
- Snapshots reuse an active stream's latest value instead of issuing reqPnL again.
- Streams survive reconnect / 1101 (re-issued) and are released by ``disconnect()``.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_connector.exchanges.interactive_brokers.exceptions import ConnectionLostError
from market_connector.exchanges.interactive_brokers.ib_gateway import IbGatewayGateway
from market_connector.exchanges.interactive_brokers.pnl import IbPnL, IbPositionPnL
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.transport import IbGatewayTransport

from ._doubles import MockIb

pytest.importorskip("ib_async")

_IB_PATH = "market_connector.exchanges.interactive_brokers.transport.IB"
ACCT = "DU1111111"
CON_ID = 265598
OTHER_CON_ID = 756733
KEY = (ACCT, "")
SKEY = (ACCT, "", CON_ID)
INITIAL_DAILY = Decimal("12.34")  # MockIb.pnl_values default
INITIAL_SINGLE_DAILY = Decimal("1.5")  # MockIb.pnl_single_values default


async def _make(*, connect: bool = True, **spec_kw: Any) -> tuple[IbGatewayTransport, MockIb]:
    ib = MockIb()
    ib.managed_accounts = [ACCT]
    with patch(_IB_PATH, return_value=ib):
        transport = IbGatewayTransport(IbConnectionSpec(**spec_kw))

    async def fast_sleep(_delay: float) -> None:
        await asyncio.sleep(0)

    transport._sleep = fast_sleep
    if connect:
        await transport.connect()
    return transport, ib


async def _settle() -> None:
    for _ in range(8):
        await asyncio.sleep(0)


def _sub_pnl(transport: IbGatewayTransport) -> tuple[Any, list[IbPnL]]:
    got: list[IbPnL] = []
    handle = transport.subscribe_pnl(got.append)  # type: ignore[attr-defined]
    return handle, got


def _sub_pos(
    transport: IbGatewayTransport, con_id: int = CON_ID
) -> tuple[Any, list[IbPositionPnL]]:
    got: list[IbPositionPnL] = []
    handle = transport.subscribe_position_pnl(con_id, got.append)  # type: ignore[attr-defined]
    return handle, got


def _daily(got: list[Any]) -> list[Decimal | None]:
    return [item.daily_pnl for item in got]


async def _drop_and_reconnect(transport: IbGatewayTransport, ib: MockIb) -> None:
    ib.simulate_drop()
    supervisor = transport._supervisor
    assert supervisor is not None
    await supervisor
    await _settle()


# --------------------------------------------------------------------------- A


class TestSubscribePnl:
    async def test_issues_one_request_and_delivers_values_in_order(self) -> None:
        transport, ib = await _make()
        _handle, got = _sub_pnl(transport)
        await _settle()
        ib.emit_pnl(dailyPnL=1.5)
        ib.emit_pnl(dailyPnL=2.5)
        assert ib.pnl_req_calls == [KEY]
        assert all(isinstance(item, IbPnL) for item in got)
        assert _daily(got) == [INITIAL_DAILY, Decimal("1.5"), Decimal("2.5")]

    async def test_values_use_snapshot_conversion(self) -> None:
        transport, _ib = await _make()
        _handle, got = _sub_pnl(transport)
        await _settle()
        assert got[0] == IbPnL(
            account=ACCT,
            daily_pnl=Decimal("12.34"),
            unrealized_pnl=Decimal("-5.5"),
            realized_pnl=Decimal("0"),
        )

    async def test_ignores_other_account_events(self) -> None:
        transport, ib = await _make()
        _handle, got = _sub_pnl(transport)
        await _settle()
        ib.emit_other_pnl()
        assert _daily(got) == [INITIAL_DAILY]

    async def test_not_connected_raises_connection_lost(self) -> None:
        transport, ib = await _make(connect=False)
        with pytest.raises(ConnectionLostError):
            transport.subscribe_pnl(lambda _p: None)  # type: ignore[attr-defined]
        assert ib.pnl_req_calls == []


# --------------------------------------------------------------------------- B


class TestClosePnl:
    async def test_close_unhooks_and_cancels_once(self) -> None:
        transport, ib = await _make()
        handle, got = _sub_pnl(transport)
        await _settle()
        handle.close()
        assert ib.pnl_cancel_calls == [KEY]
        ib.pnlEvent(ib._pnl_live[KEY])
        assert _daily(got) == [INITIAL_DAILY]

    async def test_close_is_idempotent(self) -> None:
        transport, ib = await _make()
        handle, _got = _sub_pnl(transport)
        await _settle()
        handle.close()
        handle.close()
        assert ib.pnl_cancel_calls == [KEY]


# --------------------------------------------------------------------------- C


class TestSharedPnl:
    async def test_two_subscribers_share_one_request(self) -> None:
        transport, ib = await _make()
        _h1, got1 = _sub_pnl(transport)
        _h2, got2 = _sub_pnl(transport)
        await _settle()
        ib.emit_pnl(dailyPnL=4.5)
        assert ib.pnl_req_calls == [KEY]
        assert _daily(got1) == [INITIAL_DAILY, Decimal("4.5")]
        assert _daily(got2) == [INITIAL_DAILY, Decimal("4.5")]

    async def test_cancel_only_when_last_subscriber_closes(self) -> None:
        transport, ib = await _make()
        h1, got1 = _sub_pnl(transport)
        h2, got2 = _sub_pnl(transport)
        await _settle()
        h1.close()
        assert ib.pnl_cancel_calls == []
        ib.emit_pnl(dailyPnL=6.5)
        assert _daily(got1) == [INITIAL_DAILY]
        assert _daily(got2) == [INITIAL_DAILY, Decimal("6.5")]
        h2.close()
        assert ib.pnl_cancel_calls == [KEY]

    async def test_resubscribe_after_full_close_issues_fresh_request(self) -> None:
        transport, ib = await _make()
        handle, _got = _sub_pnl(transport)
        await _settle()
        handle.close()
        _h2, got2 = _sub_pnl(transport)
        await _settle()
        assert ib.pnl_req_calls == [KEY, KEY]
        assert _daily(got2) == [INITIAL_DAILY]


# --------------------------------------------------------------------------- D


class TestSubscribePositionPnl:
    async def test_delivers_values_in_order(self) -> None:
        transport, ib = await _make()
        _handle, got = _sub_pos(transport)
        await _settle()
        ib.emit_pnl_single(CON_ID, dailyPnL=2.5)
        ib.emit_pnl_single(CON_ID, dailyPnL=3.5)
        assert ib.pnl_single_req_calls == [SKEY]
        assert all(isinstance(item, IbPositionPnL) for item in got)
        assert _daily(got) == [INITIAL_SINGLE_DAILY, Decimal("2.5"), Decimal("3.5")]
        assert got[0].con_id == CON_ID
        assert got[0].position == Decimal("100")

    async def test_ignores_other_con_id(self) -> None:
        transport, ib = await _make()
        _handle, got = _sub_pos(transport)
        await _settle()
        ib.emit_other_pnl_single(con_id=999999)
        assert _daily(got) == [INITIAL_SINGLE_DAILY]

    async def test_not_connected_raises_connection_lost(self) -> None:
        transport, ib = await _make(connect=False)
        with pytest.raises(ConnectionLostError):
            transport.subscribe_position_pnl(CON_ID, lambda _p: None)  # type: ignore[attr-defined]
        assert ib.pnl_single_req_calls == []

    async def test_close_unhooks_cancels_once_and_is_idempotent(self) -> None:
        transport, ib = await _make()
        handle, got = _sub_pos(transport)
        await _settle()
        handle.close()
        handle.close()
        assert ib.pnl_single_cancel_calls == [SKEY]
        ib.pnlSingleEvent(ib._pnl_single_live[SKEY])
        assert len(got) == 1

    async def test_same_con_id_shares_and_cancels_with_last(self) -> None:
        transport, ib = await _make()
        h1, got1 = _sub_pos(transport)
        h2, got2 = _sub_pos(transport)
        await _settle()
        assert ib.pnl_single_req_calls == [SKEY]
        h1.close()
        assert ib.pnl_single_cancel_calls == []
        ib.emit_pnl_single(CON_ID, dailyPnL=8.5)
        assert _daily(got1) == [INITIAL_SINGLE_DAILY]
        assert _daily(got2) == [INITIAL_SINGLE_DAILY, Decimal("8.5")]
        h2.close()
        assert ib.pnl_single_cancel_calls == [SKEY]

    async def test_different_con_ids_use_separate_requests(self) -> None:
        transport, ib = await _make()
        h1, got1 = _sub_pos(transport, CON_ID)
        _h2, got2 = _sub_pos(transport, OTHER_CON_ID)
        await _settle()
        assert sorted(ib.pnl_single_req_calls) == sorted([SKEY, (ACCT, "", OTHER_CON_ID)])
        assert {item.con_id for item in got1} == {CON_ID}
        assert {item.con_id for item in got2} == {OTHER_CON_ID}
        h1.close()
        assert ib.pnl_single_cancel_calls == [SKEY]


# --------------------------------------------------------------------------- E


class TestSnapshotInterplay:
    async def test_get_pnl_reuses_active_stream_value(self) -> None:
        transport, ib = await _make()
        _handle, got = _sub_pnl(transport)
        await _settle()
        ib.emit_pnl(dailyPnL=7.5)
        result = await transport.get_pnl()  # type: ignore[attr-defined]
        assert result.daily_pnl == Decimal("7.5")
        assert ib.pnl_req_calls == [KEY]
        assert ib.pnl_cancel_calls == []
        ib.emit_pnl(dailyPnL=8.5)
        assert _daily(got)[-1] == Decimal("8.5")

    async def test_get_position_pnl_reuses_active_stream_value(self) -> None:
        transport, ib = await _make()
        _handle, got = _sub_pos(transport)
        await _settle()
        ib.emit_pnl_single(CON_ID, dailyPnL=7.5)
        result = await transport.get_position_pnl(CON_ID)  # type: ignore[attr-defined]
        assert result.daily_pnl == Decimal("7.5")
        assert ib.pnl_single_req_calls == [SKEY]
        assert ib.pnl_single_cancel_calls == []
        ib.emit_pnl_single(CON_ID, dailyPnL=8.5)
        assert _daily(got)[-1] == Decimal("8.5")

    async def test_get_pnl_before_first_stream_value_waits_for_it(self) -> None:
        transport, ib = await _make()
        ib.pnl_hang = True
        _handle, got = _sub_pnl(transport)
        task = asyncio.ensure_future(transport.get_pnl())  # type: ignore[attr-defined]
        await _settle()
        assert not task.done()
        ib.emit_pnl(dailyPnL=3.5)
        result = await task
        assert result.daily_pnl == Decimal("3.5")
        assert ib.pnl_req_calls == [KEY]
        assert ib.pnl_cancel_calls == []
        assert _daily(got) == [Decimal("3.5")]

    async def test_stream_started_during_in_flight_snapshot_does_not_double_request(
        self,
    ) -> None:
        transport, ib = await _make()
        ib.pnl_hang = True
        snapshot = asyncio.ensure_future(transport.get_pnl())  # type: ignore[attr-defined]
        await _settle()
        _handle, got = _sub_pnl(transport)
        await _settle()
        assert ib.pnl_req_calls == [KEY]  # one live request; mock asserts on duplicates
        ib.emit_pnl(dailyPnL=5.5)
        result = await snapshot
        assert result.daily_pnl == Decimal("5.5")
        assert _daily(got) == [Decimal("5.5")]
        # The snapshot finishing must not tear down the stream's request.
        assert ib.pnl_cancel_calls == []
        ib.emit_pnl(dailyPnL=6.5)
        assert _daily(got) == [Decimal("5.5"), Decimal("6.5")]

    async def test_position_stream_started_during_in_flight_snapshot(self) -> None:
        transport, ib = await _make()
        ib.pnl_hang = True
        snapshot = asyncio.ensure_future(transport.get_position_pnl(CON_ID))  # type: ignore[attr-defined]
        await _settle()
        _handle, got = _sub_pos(transport)
        await _settle()
        assert ib.pnl_single_req_calls == [SKEY]
        ib.emit_pnl_single(CON_ID, dailyPnL=5.5)
        result = await snapshot
        assert result.daily_pnl == Decimal("5.5")
        assert ib.pnl_single_cancel_calls == []
        assert _daily(got) == [Decimal("5.5")]


# --------------------------------------------------------------------------- F


class TestReconnect:
    async def test_streams_survive_drop_and_reconnect(self) -> None:
        transport, ib = await _make()
        h_pnl, got = _sub_pnl(transport)
        h_pos, got_pos = _sub_pos(transport)
        await _settle()

        await _drop_and_reconnect(transport, ib)

        assert ib.pnl_req_calls == [KEY, KEY]
        assert ib.pnl_single_req_calls == [SKEY, SKEY]
        ib.emit_pnl(dailyPnL=9.5)
        ib.emit_pnl_single(CON_ID, dailyPnL=9.5)
        assert _daily(got)[-1] == Decimal("9.5")
        assert _daily(got_pos)[-1] == Decimal("9.5")

        h_pnl.close()
        h_pos.close()
        assert ib.pnl_cancel_calls == [KEY]
        assert ib.pnl_single_cancel_calls == [SKEY]

    async def test_error_1101_cancels_then_reissues(self) -> None:
        transport, ib = await _make()
        _handle, got = _sub_pnl(transport)
        _h_pos, got_pos = _sub_pos(transport)
        await _settle()

        ib.errorEvent(-1, 1101, "data lost", None)
        await _settle()

        assert ib.pnl_cancel_calls == [KEY]
        assert ib.pnl_req_calls == [KEY, KEY]
        assert ib.pnl_single_cancel_calls == [SKEY]
        assert ib.pnl_single_req_calls == [SKEY, SKEY]
        ib.emit_pnl(dailyPnL=9.5)
        ib.emit_pnl_single(CON_ID, dailyPnL=9.5)
        assert _daily(got)[-1] == Decimal("9.5")
        assert _daily(got_pos)[-1] == Decimal("9.5")


# --------------------------------------------------------------------------- G


class TestDisconnectReleasesStreams:
    async def test_disconnect_cancels_each_key_once_and_close_is_noop(self) -> None:
        transport, ib = await _make()
        h_pnl, _got = _sub_pnl(transport)
        h_pos, _got_pos = _sub_pos(transport)
        await _settle()

        await transport.disconnect()

        assert ib.pnl_cancel_calls == [KEY]
        assert ib.pnl_single_cancel_calls == [SKEY]
        h_pnl.close()
        h_pos.close()
        assert ib.pnl_cancel_calls == [KEY]
        assert ib.pnl_single_cancel_calls == [SKEY]


# --------------------------------------------------------------------------- H


class TestGatewayPnlStream:
    async def test_subscribe_pnl_delegates(self) -> None:
        handle = object()
        transport = MagicMock()
        transport.subscribe_pnl = MagicMock(return_value=handle)
        gateway = IbGatewayGateway(transport=transport, signer=MagicMock())
        callback = MagicMock()
        assert gateway.subscribe_pnl(callback) is handle  # type: ignore[attr-defined]
        transport.subscribe_pnl.assert_called_once_with(callback)

    async def test_subscribe_position_pnl_resolves_then_delegates(self) -> None:
        handle = object()
        transport = MagicMock()
        transport.subscribe_position_pnl = MagicMock(return_value=handle)
        resolver = MagicMock()
        resolved = MagicMock()
        resolved.native = SimpleNamespace(conId=CON_ID)
        resolver.resolve = AsyncMock(return_value=resolved)
        gateway = IbGatewayGateway(
            transport=transport, signer=MagicMock(), contract_resolver=resolver
        )
        ref = MagicMock()
        callback = MagicMock()

        result = await gateway.subscribe_position_pnl(ref, callback)  # type: ignore[attr-defined]

        assert result is handle
        resolver.resolve.assert_awaited_once_with(ref)
        transport.subscribe_position_pnl.assert_called_once_with(CON_ID, callback)

    async def test_subscribe_position_pnl_without_resolver_raises(self) -> None:
        transport = MagicMock()
        gateway = IbGatewayGateway(transport=transport, signer=MagicMock())
        with pytest.raises(RuntimeError, match="contract_resolver"):
            await gateway.subscribe_position_pnl(MagicMock(), MagicMock())  # type: ignore[attr-defined]
        transport.subscribe_position_pnl.assert_not_called()
