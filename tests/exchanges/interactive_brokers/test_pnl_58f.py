"""Acceptance tests for issue #58f: IB PnL snapshots via reqPnL / reqPnLSingle.

Red phase: the feature does not exist yet, so the not-yet-existing names are looked up
lazily (helpers below) to make each test fail on its own.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import math
import sys
import time
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_connector.exchanges.interactive_brokers.exceptions import (
    ConnectionLostError,
    IbError,
)
from market_connector.exchanges.interactive_brokers.ib_gateway import IbGatewayGateway
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.transport import IbGatewayTransport

from ._doubles import MockIb

pytest.importorskip("ib_async")

_IB_PATH = "market_connector.exchanges.interactive_brokers.transport.IB"
_PKG = "market_connector.exchanges.interactive_brokers"
ACCT = "DU1111111"
CON_ID = 265598
UNSET_DOUBLE = sys.float_info.max


def _pnl_mod() -> Any:
    return importlib.import_module(f"{_PKG}.pnl")


def _pnl_timeout_error() -> type[Exception]:
    return getattr(importlib.import_module(f"{_PKG}.exceptions"), "PnlTimeoutError")  # noqa: B009


def _real_pnl(**kw: Any) -> Any:
    from ib_async import PnL  # noqa: PLC0415

    pnl = PnL(ACCT, "")
    for name, value in kw.items():
        setattr(pnl, name, value)
    return pnl


def _real_pnl_single(**kw: Any) -> Any:
    from ib_async import PnLSingle  # noqa: PLC0415

    pnl = PnLSingle(ACCT, "", CON_ID)
    for name, value in kw.items():
        setattr(pnl, name, value)
    return pnl


async def _make(*, connect: bool = True, **spec_kw: Any) -> tuple[IbGatewayTransport, MockIb]:
    ib = MockIb()
    ib.managed_accounts = [ACCT]
    with patch(_IB_PATH, return_value=ib):
        transport = IbGatewayTransport(IbConnectionSpec(**spec_kw))
    if connect:
        await transport.connect()
    return transport, ib


async def _let_requests_start() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


# --------------------------------------------------------------------------- A


class TestIbPnLType:
    def test_conversion(self) -> None:
        result = _pnl_mod().IbPnL.from_pnl(
            _real_pnl(dailyPnL=12.34, unrealizedPnL=-5.5, realizedPnL=0.0)
        )
        assert result.account == ACCT
        assert result.daily_pnl == Decimal("12.34")
        assert result.unrealized_pnl == Decimal("-5.5")
        assert result.realized_pnl == Decimal("0")
        assert result.realized_pnl is not None

    def test_nan_and_unset_sentinel_are_none(self) -> None:
        result = _pnl_mod().IbPnL.from_pnl(_real_pnl(dailyPnL=math.nan, unrealizedPnL=UNSET_DOUBLE))
        assert result.daily_pnl is None
        assert result.unrealized_pnl is None
        assert result.realized_pnl is None  # default nan

    def test_is_frozen(self) -> None:
        result = _pnl_mod().IbPnL.from_pnl(_real_pnl(dailyPnL=1.0))
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.daily_pnl = Decimal(2)

    def test_fields(self) -> None:
        names = {f.name for f in dataclasses.fields(_pnl_mod().IbPnL)}
        assert names == {"account", "daily_pnl", "unrealized_pnl", "realized_pnl"}


class TestIbPositionPnLType:
    def test_conversion(self) -> None:
        result = _pnl_mod().IbPositionPnL.from_pnl_single(
            _real_pnl_single(
                position=100,
                dailyPnL=12.34,
                unrealizedPnL=-5.5,
                realizedPnL=0.0,
                value=15000.5,
            )
        )
        assert result.account == ACCT
        assert result.con_id == CON_ID
        assert result.position == Decimal("100")
        assert result.daily_pnl == Decimal("12.34")
        assert result.unrealized_pnl == Decimal("-5.5")
        assert result.realized_pnl == Decimal("0")
        assert result.realized_pnl is not None
        assert result.value == Decimal("15000.5")

    def test_negative_position(self) -> None:
        result = _pnl_mod().IbPositionPnL.from_pnl_single(_real_pnl_single(position=-3))
        assert result.position == Decimal("-3")

    def test_nan_and_unset_sentinel_are_none(self) -> None:
        result = _pnl_mod().IbPositionPnL.from_pnl_single(
            _real_pnl_single(position=1, dailyPnL=math.nan, value=UNSET_DOUBLE)
        )
        assert result.daily_pnl is None
        assert result.value is None
        assert result.unrealized_pnl is None

    def test_is_frozen(self) -> None:
        result = _pnl_mod().IbPositionPnL.from_pnl_single(_real_pnl_single(position=1))
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.position = Decimal(2)

    def test_fields(self) -> None:
        names = {f.name for f in dataclasses.fields(_pnl_mod().IbPositionPnL)}
        assert names == {
            "account",
            "con_id",
            "position",
            "daily_pnl",
            "unrealized_pnl",
            "realized_pnl",
            "value",
        }


# --------------------------------------------------------------------------- D


class TestSpecPnlTimeout:
    def test_default_is_ten_seconds(self) -> None:
        assert IbConnectionSpec().pnl_timeout == 10.0  # type: ignore[attr-defined]

    @pytest.mark.parametrize("value", [0, 0.0, -1.0])
    def test_rejects_non_positive(self, value: float) -> None:
        with pytest.raises(ValueError, match="pnl_timeout"):
            IbConnectionSpec(pnl_timeout=value)  # type: ignore[call-arg]

    def test_accepts_small_positive(self) -> None:
        assert IbConnectionSpec(pnl_timeout=0.05).pnl_timeout == 0.05  # type: ignore[call-arg,attr-defined]


class TestPnlTimeoutError:
    def test_subclasses_ib_error(self) -> None:
        assert issubclass(_pnl_timeout_error(), IbError)


# --------------------------------------------------------------------------- B


class TestTransportGetPnl:
    async def test_snapshot_requests_then_cancels_once(self) -> None:
        transport, ib = await _make()
        result = await transport.get_pnl()  # type: ignore[attr-defined]
        assert ib.pnl_req_calls == [(ACCT, "")]
        assert ib.pnl_cancel_calls == [(ACCT, "")]
        assert result == _pnl_mod().IbPnL(
            account=ACCT,
            daily_pnl=Decimal("12.34"),
            unrealized_pnl=Decimal("-5.5"),
            realized_pnl=Decimal("0"),
        )

    async def test_ignores_event_for_other_account(self) -> None:
        transport, ib = await _make()
        task = asyncio.ensure_future(transport.get_pnl())  # type: ignore[attr-defined]
        await _let_requests_start()
        ib.emit_other_pnl()
        result = await task
        assert result.account == ACCT
        assert result.daily_pnl == Decimal("12.34")

    async def test_never_connected_raises_connection_lost(self) -> None:
        transport, ib = await _make(connect=False)
        with pytest.raises(ConnectionLostError):
            await transport.get_pnl()  # type: ignore[attr-defined]
        assert ib.pnl_req_calls == []

    async def test_after_disconnect_raises_connection_lost(self) -> None:
        transport, ib = await _make()
        await transport.disconnect()
        with pytest.raises(ConnectionLostError):
            await transport.get_pnl()  # type: ignore[attr-defined]
        assert ib.pnl_req_calls == []

    async def test_timeout_raises_and_cancels(self) -> None:
        transport, ib = await _make(pnl_timeout=0.05)
        ib.pnl_hang = True
        started = time.monotonic()
        with pytest.raises(_pnl_timeout_error()):
            await transport.get_pnl()  # type: ignore[attr-defined]
        assert time.monotonic() - started < 1.0
        assert ib.pnl_cancel_calls == [(ACCT, "")]

    async def test_caller_cancellation_cancels_subscription(self) -> None:
        transport, ib = await _make()
        ib.pnl_hang = True
        task = asyncio.ensure_future(transport.get_pnl())  # type: ignore[attr-defined]
        await _let_requests_start()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ib.pnl_cancel_calls == [(ACCT, "")]

    async def test_concurrent_calls_share_one_request(self) -> None:
        transport, ib = await _make()
        first, second = await asyncio.gather(
            transport.get_pnl(),  # type: ignore[attr-defined]
            transport.get_pnl(),  # type: ignore[attr-defined]
        )
        assert first == second
        assert len(ib.pnl_req_calls) == 1

    async def test_sequential_calls_issue_fresh_requests(self) -> None:
        transport, ib = await _make()
        await transport.get_pnl()  # type: ignore[attr-defined]
        await transport.get_pnl()  # type: ignore[attr-defined]
        assert len(ib.pnl_req_calls) == 2
        assert len(ib.pnl_cancel_calls) == 2


# --------------------------------------------------------------------------- C


class TestTransportGetPositionPnl:
    async def test_snapshot_requests_then_cancels_once(self) -> None:
        transport, ib = await _make()
        result = await transport.get_position_pnl(CON_ID)  # type: ignore[attr-defined]
        assert ib.pnl_single_req_calls == [(ACCT, "", CON_ID)]
        assert ib.pnl_single_cancel_calls == [(ACCT, "", CON_ID)]
        assert result == _pnl_mod().IbPositionPnL(
            account=ACCT,
            con_id=CON_ID,
            position=Decimal("100"),
            daily_pnl=Decimal("1.5"),
            unrealized_pnl=Decimal("20.25"),
            realized_pnl=None,
            value=Decimal("15000.0"),
        )

    async def test_ignores_event_for_other_con_id(self) -> None:
        transport, ib = await _make()
        task = asyncio.ensure_future(transport.get_position_pnl(CON_ID))  # type: ignore[attr-defined]
        await _let_requests_start()
        ib.emit_other_pnl_single(con_id=999999)
        result = await task
        assert result.con_id == CON_ID
        assert result.position == Decimal("100")

    async def test_not_connected_raises_connection_lost(self) -> None:
        transport, ib = await _make(connect=False)
        with pytest.raises(ConnectionLostError):
            await transport.get_position_pnl(CON_ID)  # type: ignore[attr-defined]
        assert ib.pnl_single_req_calls == []

    async def test_timeout_raises_and_cancels(self) -> None:
        transport, ib = await _make(pnl_timeout=0.05)
        ib.pnl_hang = True
        with pytest.raises(_pnl_timeout_error()):
            await transport.get_position_pnl(CON_ID)  # type: ignore[attr-defined]
        assert ib.pnl_single_cancel_calls == [(ACCT, "", CON_ID)]

    async def test_concurrent_same_con_id_shares_one_request(self) -> None:
        transport, ib = await _make()
        first, second = await asyncio.gather(
            transport.get_position_pnl(CON_ID),  # type: ignore[attr-defined]
            transport.get_position_pnl(CON_ID),  # type: ignore[attr-defined]
        )
        assert first == second
        assert len(ib.pnl_single_req_calls) == 1

    async def test_concurrent_different_con_ids_request_each(self) -> None:
        transport, ib = await _make()
        first, second = await asyncio.gather(
            transport.get_position_pnl(CON_ID),  # type: ignore[attr-defined]
            transport.get_position_pnl(756733),  # type: ignore[attr-defined]
        )
        assert sorted(ib.pnl_single_req_calls) == [(ACCT, "", 756733), (ACCT, "", CON_ID)]
        assert first.con_id == CON_ID
        assert second.con_id == 756733


# --------------------------------------------------------------------------- E


class TestGatewayPnl:
    async def test_get_pnl_delegates(self) -> None:
        sentinel = object()
        transport = MagicMock()
        transport.get_pnl = AsyncMock(return_value=sentinel)
        gateway = IbGatewayGateway(transport=transport, signer=MagicMock())
        assert await gateway.get_pnl() is sentinel  # type: ignore[attr-defined]
        transport.get_pnl.assert_awaited_once_with()

    async def test_get_position_pnl_resolves_then_delegates(self) -> None:
        sentinel = object()
        transport = MagicMock()
        transport.get_position_pnl = AsyncMock(return_value=sentinel)
        resolver = MagicMock()
        resolved = MagicMock()
        resolved.native = SimpleNamespace(conId=CON_ID)
        resolver.resolve = AsyncMock(return_value=resolved)
        gateway = IbGatewayGateway(
            transport=transport, signer=MagicMock(), contract_resolver=resolver
        )
        ref = MagicMock()

        result = await gateway.get_position_pnl(ref)  # type: ignore[attr-defined]

        assert result is sentinel
        resolver.resolve.assert_awaited_once_with(ref)
        transport.get_position_pnl.assert_awaited_once_with(CON_ID)

    async def test_get_position_pnl_without_resolver_raises(self) -> None:
        transport = MagicMock()
        transport.get_position_pnl = AsyncMock()
        gateway = IbGatewayGateway(transport=transport, signer=MagicMock())
        with pytest.raises(RuntimeError, match="contract_resolver"):
            await gateway.get_position_pnl(MagicMock())  # type: ignore[attr-defined]
        transport.get_position_pnl.assert_not_awaited()
