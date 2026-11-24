"""RED acceptance tests for #249 (Interactive Brokers): per-leg ``LegFill`` emission.

Contract (``market_connector.protocols``): ``subscribe_leg_fills(callback) -> unsubscribe``; the
callback (sync or async) receives a ``LegFill`` whose ``cumulative_filled`` is strictly increasing
per ``client_order_id``. Lower, equal or zero values emit nothing.

IB keying: ``client_order_id`` is the link tag carried in ``Execution.orderRef`` (not the numeric
orderId), so legs of orders placed by a previous process are still attributed after a restart.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_connector.exchanges.interactive_brokers.ib_gateway import IbGatewayGateway
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.transport import IbGatewayTransport
from market_connector.orders import HBOrder, OrderType, TradeType
from market_connector.protocols import LegFill, SupportsLegFills

from ._doubles import MockIb, make_fill

pytest.importorskip("ib_async")

_IB_PATH = "market_connector.exchanges.interactive_brokers.transport.IB"
ACCT = "DU1111111"
_EXEC_TS = datetime(2026, 1, 2, 15, 30, tzinfo=timezone.utc).timestamp()  # noqa: UP017
TAG_A = "mcl-5-0"
TAG_B = "mcl-5-1"


class AckIb(MockIb):
    """MockIb that acknowledges every placed order with ``Submitted``."""

    def placeOrder(self, contract: Any, order: Any) -> Any:  # noqa: N802
        trade = super().placeOrder(contract, order)
        asyncio.get_running_loop().call_later(
            0.02, self.emit_status, trade.order.orderId, "Submitted"
        )
        return trade


async def _gateway() -> tuple[IbGatewayGateway, AckIb]:
    ib = AckIb()
    ib.managed_accounts = [ACCT]
    ib.client.next_req_id = 500
    with patch(_IB_PATH, return_value=ib):
        transport = IbGatewayTransport(IbConnectionSpec(account_id=ACCT))
    await transport.connect()
    transport._first_status_timeout = 0.5
    native = SimpleNamespace(symbol="AAPL", secType="STK", conId=1)
    resolver = MagicMock()
    resolver.resolve = AsyncMock(return_value=SimpleNamespace(native=native))
    resolver.resolve_from_pair = AsyncMock(return_value=SimpleNamespace(native=native))
    gateway = IbGatewayGateway(transport=transport, signer=MagicMock(), contract_resolver=resolver)
    return gateway, ib


def _limit() -> HBOrder:
    return HBOrder(
        order_type=OrderType.LIMIT,
        side=TradeType.BUY,
        amount=Decimal(10),
        price=Decimal(100),
    )


async def _place(gateway: IbGatewayGateway, ib: AckIb, tag: str | None) -> int:
    """Place a (link-tagged) leg of qty 10 and return its IB orderId."""
    if tag is None:
        await gateway.place_order(MagicMock(), _limit())
    else:
        await gateway.place_order(MagicMock(), _limit(), link_tag=tag)  # type: ignore[call-arg]
    return int(ib.placed_orders[-1].orderId)


def _exec(
    ib: MockIb,
    exec_id: str,
    order_id: int,
    cum: float,
    *,
    tag: str,
    price: float = 101.25,
    shares: float | None = None,
) -> None:
    fill = make_fill(
        exec_id,
        order_id=order_id,
        cum_qty=cum,
        price=price,
        shares=cum if shares is None else shares,
        order_ref=tag,
    )
    ib.execDetailsEvent(None, fill)


async def _settle() -> None:
    await asyncio.sleep(0.01)


class TestExportsAndProtocol:
    async def test_gateway_satisfies_supports_leg_fills(self) -> None:
        gateway, _ = await _gateway()

        assert isinstance(gateway, SupportsLegFills)


class TestEmission:
    async def test_two_executions_emit_two_leg_fills(self) -> None:
        gateway, ib = await _gateway()
        oid = await _place(gateway, ib, TAG_A)
        got: list[LegFill] = []
        gateway.subscribe_leg_fills(got.append)

        _exec(ib, "e1.01", oid, 4, tag=TAG_A, price=101.25, shares=4)
        _exec(ib, "e2.01", oid, 10, tag=TAG_A, price=102.5, shares=6)

        assert [f.cumulative_filled for f in got] == [Decimal(4), Decimal(10)]

    async def test_fields_are_populated_from_execution(self) -> None:
        gateway, ib = await _gateway()
        oid = await _place(gateway, ib, TAG_A)
        got: list[LegFill] = []
        gateway.subscribe_leg_fills(got.append)

        _exec(ib, "e1.01", oid, 4, tag=TAG_A, price=101.25, shares=4)

        (fill,) = got
        assert fill.client_order_id == TAG_A
        assert fill.client_order_id != str(oid)
        assert fill.fill_id == "e1.01"
        assert fill.last_price == Decimal("101.25")
        assert fill.timestamp == _EXEC_TS

    async def test_values_are_decimal_not_float(self) -> None:
        gateway, ib = await _gateway()
        oid = await _place(gateway, ib, TAG_A)
        got: list[LegFill] = []
        gateway.subscribe_leg_fills(got.append)

        _exec(ib, "e1.01", oid, 0.3, tag=TAG_A, price=0.1, shares=0.3)

        (fill,) = got
        assert isinstance(fill.cumulative_filled, Decimal)
        assert isinstance(fill.last_price, Decimal)
        assert fill.cumulative_filled == Decimal("0.3")
        assert fill.last_price == Decimal("0.1")

    async def test_async_callback_is_awaited(self) -> None:
        gateway, ib = await _gateway()
        oid = await _place(gateway, ib, TAG_A)
        got: list[LegFill] = []

        async def cb(fill: LegFill) -> None:
            await asyncio.sleep(0)
            got.append(fill)

        gateway.subscribe_leg_fills(cb)
        _exec(ib, "e1.01", oid, 4, tag=TAG_A)
        await _settle()

        assert [f.cumulative_filled for f in got] == [Decimal(4)]

    async def test_two_legs_accumulate_independently(self) -> None:
        gateway, ib = await _gateway()
        oid_a = await _place(gateway, ib, TAG_A)
        oid_b = await _place(gateway, ib, TAG_B)
        got: list[LegFill] = []
        gateway.subscribe_leg_fills(got.append)

        _exec(ib, "a1.01", oid_a, 4, tag=TAG_A)
        _exec(ib, "b1.01", oid_b, 3, tag=TAG_B)
        _exec(ib, "a2.01", oid_a, 10, tag=TAG_A)
        _exec(ib, "b2.01", oid_b, 5, tag=TAG_B)

        assert [(f.client_order_id, f.cumulative_filled) for f in got] == [
            (TAG_A, Decimal(4)),
            (TAG_B, Decimal(3)),
            (TAG_A, Decimal(10)),
            (TAG_B, Decimal(5)),
        ]


class TestNoDuplicateEmission:
    async def test_same_exec_id_twice_emits_once(self) -> None:
        gateway, ib = await _gateway()
        oid = await _place(gateway, ib, TAG_A)
        got: list[LegFill] = []
        gateway.subscribe_leg_fills(got.append)

        _exec(ib, "e1.01", oid, 4, tag=TAG_A)
        _exec(ib, "e1.01", oid, 4, tag=TAG_A)

        assert [f.cumulative_filled for f in got] == [Decimal(4)]

    async def test_correction_with_unchanged_cum_qty_emits_nothing(self) -> None:
        gateway, ib = await _gateway()
        oid = await _place(gateway, ib, TAG_A)
        got: list[LegFill] = []
        gateway.subscribe_leg_fills(got.append)

        _exec(ib, "e1.01", oid, 4, tag=TAG_A)
        _exec(ib, "e1.02", oid, 4, tag=TAG_A)

        assert [f.cumulative_filled for f in got] == [Decimal(4)]

    async def test_lower_cum_qty_emits_nothing(self) -> None:
        gateway, ib = await _gateway()
        oid = await _place(gateway, ib, TAG_A)
        got: list[LegFill] = []
        gateway.subscribe_leg_fills(got.append)

        _exec(ib, "e1.01", oid, 10, tag=TAG_A)
        _exec(ib, "e2.01", oid, 7, tag=TAG_A)

        assert [f.cumulative_filled for f in got] == [Decimal(10)]

    async def test_get_fills_replay_emits_nothing_new(self) -> None:
        gateway, ib = await _gateway()
        oid = await _place(gateway, ib, TAG_A)
        got: list[LegFill] = []
        gateway.subscribe_leg_fills(got.append)
        live = make_fill("e1.01", order_id=oid, cum_qty=4, shares=4, order_ref=TAG_A)
        ib.execDetailsEvent(None, live)
        ib.executions_result = [live]

        await gateway.get_fills(datetime(2026, 1, 1, tzinfo=timezone.utc))  # noqa: UP017

        assert [f.cumulative_filled for f in got] == [Decimal(4)]

    async def test_reconcile_replay_after_reconnect_emits_nothing_new(self) -> None:
        gateway, ib = await _gateway()
        oid = await _place(gateway, ib, TAG_A)
        got: list[LegFill] = []
        gateway.subscribe_leg_fills(got.append)
        live = make_fill("e1.01", order_id=oid, cum_qty=4, shares=4, order_ref=TAG_A)
        ib.execDetailsEvent(None, live)
        ib.executions_result = [live]

        transport = gateway._transport  # noqa: SLF001
        ib.simulate_drop()
        assert transport._supervisor is not None  # noqa: SLF001
        await transport._supervisor  # noqa: SLF001

        assert [f.cumulative_filled for f in got] == [Decimal(4)]


class TestKeying:
    async def test_restart_keyed_by_execution_order_ref_ASSUMES_312(self) -> None:  # noqa: N802
        """ASSUMPTION: Execution.orderRef carries the link tag for an order not placed in-process.

        This depends on orderRef surviving onto executions, which is unverified per #312/#319.
        The live probe for #312 will confirm or break this test.
        """
        gateway, ib = await _gateway()
        got: list[LegFill] = []
        gateway.subscribe_leg_fills(got.append)

        _exec(ib, "r1.01", 9999, 4, tag="mcl-9-0")

        assert [(f.client_order_id, f.cumulative_filled) for f in got] == [("mcl-9-0", Decimal(4))]

    async def test_untagged_order_emits_nothing(self) -> None:
        gateway, ib = await _gateway()
        oid = await _place(gateway, ib, None)
        got: list[LegFill] = []
        gateway.subscribe_leg_fills(got.append)

        _exec(ib, "u1.01", oid, 4, tag="")
        _exec(ib, "u2.01", 4242, 5, tag="")

        assert got == []


class TestFallbackKey:
    """In-session keying must not depend on Execution.orderRef surviving."""

    async def test_empty_order_ref_keyed_by_matching_order_id(self) -> None:
        gateway, ib = await _gateway()
        oid = await _place(gateway, ib, TAG_A)
        got: list[LegFill] = []
        gateway.subscribe_leg_fills(got.append)

        _exec(ib, "e1.01", oid, 4, tag="")

        assert [(f.client_order_id, f.cumulative_filled) for f in got] == [(TAG_A, Decimal(4))]

    async def test_replay_with_zero_order_id_keyed_by_perm_id(self) -> None:
        gateway, ib = await _gateway()
        oid = await _place(gateway, ib, TAG_A)
        ib.emit_status(oid, "Submitted", perm_id=777001)
        got: list[LegFill] = []
        gateway.subscribe_leg_fills(got.append)

        replay = make_fill("r1.01", order_id=0, cum_qty=4, shares=4, order_ref="", perm_id=777001)
        ib.execDetailsEvent(None, replay)

        assert [(f.client_order_id, f.cumulative_filled) for f in got] == [(TAG_A, Decimal(4))]

    async def test_perm_id_replay_after_live_emit_same_cum_qty_emits_nothing(self) -> None:
        gateway, ib = await _gateway()
        oid = await _place(gateway, ib, TAG_A)
        ib.emit_status(oid, "Submitted", perm_id=777001)
        got: list[LegFill] = []
        gateway.subscribe_leg_fills(got.append)
        _exec(ib, "e1.01", oid, 4, tag=TAG_A)

        replay = make_fill("r1.01", order_id=0, cum_qty=4, shares=4, order_ref="", perm_id=777001)
        ib.execDetailsEvent(None, replay)

        assert [f.cumulative_filled for f in got] == [Decimal(4)]


class TestZeroCumulativeNotEmitted:
    async def test_zero_cum_qty_emits_nothing(self) -> None:
        gateway, ib = await _gateway()
        oid = await _place(gateway, ib, TAG_A)
        got: list[LegFill] = []
        gateway.subscribe_leg_fills(got.append)

        _exec(ib, "z1.01", oid, 0, tag=TAG_A, shares=0)

        assert got == []


class TestLifecycle:
    async def test_unsubscribe_stops_delivery_and_is_idempotent(self) -> None:
        gateway, ib = await _gateway()
        oid = await _place(gateway, ib, TAG_A)
        got: list[LegFill] = []
        unsubscribe = gateway.subscribe_leg_fills(got.append)

        _exec(ib, "e1.01", oid, 4, tag=TAG_A)
        unsubscribe()
        unsubscribe()
        _exec(ib, "e2.01", oid, 10, tag=TAG_A)

        assert [f.cumulative_filled for f in got] == [Decimal(4)]

    async def test_raising_callback_is_swallowed_and_others_still_receive(self) -> None:
        gateway, ib = await _gateway()
        oid = await _place(gateway, ib, TAG_A)
        got: list[LegFill] = []

        def boom(_fill: LegFill) -> None:
            raise RuntimeError("subscriber failure")

        gateway.subscribe_leg_fills(boom)
        gateway.subscribe_leg_fills(got.append)

        _exec(ib, "e1.01", oid, 4, tag=TAG_A)

        assert [f.cumulative_filled for f in got] == [Decimal(4)]
