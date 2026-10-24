"""Acceptance tests for issue #58e: IB bracket and OCA orders (red phase)."""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
from collections.abc import Mapping
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_connector.exchanges.interactive_brokers.exceptions import (
    ConnectionLostError,
    OrderRejectedError,
)
from market_connector.exchanges.interactive_brokers.ib_gateway import IbGatewayGateway
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.transport import IbGatewayTransport
from market_connector.orders import HBOrder, OrderType, TimeInForce, TradeType

from ._doubles import MockIb

pytest.importorskip("ib_async")

_IB_PATH = "market_connector.exchanges.interactive_brokers.transport.IB"
_PKG = "market_connector.exchanges.interactive_brokers"
ACCT = "DU1111111"
FIRST_ID = 500


def _linked() -> Any:
    # Looked up lazily so collection succeeds while the module is missing (red phase).
    return importlib.import_module(f"{_PKG}.linked_orders")


def _linked_error() -> type[Exception]:
    return getattr(importlib.import_module(f"{_PKG}.exceptions"), "LinkedOrderRejectedError")  # noqa: B009


class AutoCancelIb(MockIb):
    """MockIb whose accepted cancels are confirmed with a Cancelled status."""

    def cancelOrder(self, order: Any) -> None:  # noqa: N802
        super().cancelOrder(order)
        asyncio.get_running_loop().call_soon(self.emit_status, order.orderId, "Cancelled")


def _hb(
    order_type: OrderType,
    side: TradeType,
    *,
    price: str | None = None,
    stop: str | None = None,
    amount: str = "10",
    **kw: Any,
) -> HBOrder:
    return HBOrder(
        order_type=order_type,
        side=side,
        amount=Decimal(amount),
        price=Decimal(price) if price else None,
        stop_price=Decimal(stop) if stop else None,
        **kw,
    )


def _entry(**kw: Any) -> HBOrder:
    return _hb(OrderType.LIMIT, TradeType.BUY, price="100", **kw)


def _tp(**kw: Any) -> HBOrder:
    return _hb(OrderType.LIMIT, TradeType.SELL, price="110", **kw)


def _sl(**kw: Any) -> HBOrder:
    return _hb(OrderType.STOP_LOSS, TradeType.SELL, stop="95", **kw)


async def _make(*, connect: bool = True) -> tuple[IbGatewayTransport, AutoCancelIb]:
    ib = AutoCancelIb()
    ib.managed_accounts = [ACCT]
    ib.client.next_req_id = FIRST_ID
    with patch(_IB_PATH, return_value=ib):
        transport = IbGatewayTransport(IbConnectionSpec(account_id=ACCT))
    if connect:
        await transport.connect()
    transport._first_status_timeout = 0.5
    return transport, ib


async def _when_placed(ib: MockIb, count: int) -> None:
    for _ in range(500):
        if len(ib.placed_orders) >= count:
            return
        await asyncio.sleep(0.002)
    raise AssertionError(f"only {len(ib.placed_orders)} of {count} orders were placed")


async def _ack_all(ib: MockIb, count: int, statuses: list[str] | None = None) -> None:
    """After *count* placements, acknowledge each order (default Submitted)."""
    await _when_placed(ib, count)
    for order, status in zip(
        ib.placed_orders[:count], statuses or ["Submitted"] * count, strict=True
    ):
        ib.emit_status(order.orderId, status)


async def _ack_all_from(ib: MockIb, start: int, count: int) -> None:
    """Acknowledge orders ``start..count`` once *count* orders have been placed in total."""
    await _when_placed(ib, count)
    for order in ib.placed_orders[start:count]:
        ib.emit_status(order.orderId, "Submitted")


def _ids(items: Any) -> set[int]:
    """Order ids from a Mapping/tuple of handles, ids, or (handle, error) pairs."""
    keys = items.keys() if isinstance(items, Mapping) else items
    out = set()
    for item in keys:
        if isinstance(item, tuple):
            item = item[0]  # noqa: PLW2901
        out.add(getattr(item, "order_id", item))
    return out


# --------------------------------------------------------------------------- A / B


class TestLinkedOrderTypes:
    def test_oca_type_values(self) -> None:
        oca_type = _linked().OcaType
        assert oca_type.CANCEL_WITH_BLOCK == 1
        assert oca_type.REDUCE_WITH_BLOCK == 2
        assert oca_type.REDUCE_NO_BLOCK == 3
        assert isinstance(oca_type.CANCEL_WITH_BLOCK, int)

    def test_ib_bracket_is_frozen_dataclass(self) -> None:
        cls = _linked().IbBracket
        assert dataclasses.is_dataclass(cls)
        assert [f.name for f in dataclasses.fields(cls)] == ["parent", "take_profit", "stop_loss"]
        inst = cls(parent=1, take_profit=2, stop_loss=3)
        with pytest.raises(dataclasses.FrozenInstanceError):
            inst.parent = 9

    def test_ib_oca_group_is_frozen_dataclass(self) -> None:
        mod = _linked()
        assert dataclasses.is_dataclass(mod.IbOcaGroup)
        assert [f.name for f in dataclasses.fields(mod.IbOcaGroup)] == [
            "group",
            "oca_type",
            "orders",
        ]
        inst = mod.IbOcaGroup(group="g", oca_type=mod.OcaType.CANCEL_WITH_BLOCK, orders=())
        with pytest.raises(dataclasses.FrozenInstanceError):
            inst.group = "other"

    def test_linked_order_rejected_error_is_order_rejected(self) -> None:
        cls = _linked_error()
        assert issubclass(cls, OrderRejectedError)


# --------------------------------------------------------------------------- C


class TestPlaceBracket:
    async def test_ids_order_and_transmit_chain(self) -> None:
        transport, ib = await _make()
        _, result = await asyncio.gather(
            _ack_all(ib, 3), transport.place_bracket(MagicMock(), _entry(), _tp(), _sl())
        )
        assert ib.client.req_id_calls == 3
        orders = ib.placed_orders
        assert [o.orderId for o in orders] == [FIRST_ID, FIRST_ID + 1, FIRST_ID + 2]
        assert [o.transmit for o in orders] == [False, False, True]
        assert [o.parentId for o in orders] == [0, FIRST_ID, FIRST_ID]
        assert isinstance(result, _linked().IbBracket)
        assert result.parent.order_id == FIRST_ID
        assert result.take_profit.order_id == FIRST_ID + 1
        assert result.stop_loss.order_id == FIRST_ID + 2

    async def test_leg_fields_from_hb_orders(self) -> None:
        transport, ib = await _make()
        entry = _entry(time_in_force=TimeInForce.GTC, outside_rth=True)
        take_profit = _tp(time_in_force=TimeInForce.GTC, outside_rth=True)
        stop = _sl(time_in_force=TimeInForce.GTC, outside_rth=True)
        await asyncio.gather(
            _ack_all(ib, 3), transport.place_bracket(MagicMock(), entry, take_profit, stop)
        )
        parent, tp, sl = ib.placed_orders
        assert (parent.action, parent.orderType, parent.lmtPrice) == ("BUY", "LMT", 100.0)
        assert (tp.action, tp.orderType, tp.lmtPrice) == ("SELL", "LMT", 110.0)
        assert (sl.action, sl.orderType, sl.auxPrice) == ("SELL", "STP", 95.0)
        for order in ib.placed_orders:
            assert order.totalQuantity == 10.0
            assert order.tif == "GTC"
            assert order.outsideRth is True

    async def test_market_entry_and_stop_limit_stop(self) -> None:
        transport, ib = await _make()
        entry = _hb(OrderType.MARKET, TradeType.BUY)
        stop = _hb(OrderType.STOP_LOSS_LIMIT, TradeType.SELL, price="94", stop="95")
        await asyncio.gather(
            _ack_all(ib, 3), transport.place_bracket(MagicMock(), entry, _tp(), stop)
        )
        parent, _, sl = ib.placed_orders
        assert parent.orderType == "MKT"
        assert (sl.orderType, sl.lmtPrice, sl.auxPrice) == ("STP LMT", 94.0, 95.0)

    async def test_account_stamped_on_all_legs(self) -> None:
        transport, ib = await _make()
        await asyncio.gather(
            _ack_all(ib, 3), transport.place_bracket(MagicMock(), _entry(), _tp(), _sl())
        )
        assert [o.account for o in ib.placed_orders] == [ACCT] * 3

    async def test_contract_passed_to_every_leg(self) -> None:
        transport, ib = await _make()
        native = MagicMock()
        await asyncio.gather(
            _ack_all(ib, 3), transport.place_bracket(native, _entry(), _tp(), _sl())
        )
        assert ib.placed_contracts == [native] * 3

    async def test_handles_registered(self) -> None:
        transport, ib = await _make()
        _, result = await asyncio.gather(
            _ack_all(ib, 3, ["PreSubmitted", "Submitted", "Submitted"]),
            transport.place_bracket(MagicMock(), _entry(), _tp(), _sl()),
        )
        for handle in (result.parent, result.take_profit, result.stop_loss):
            assert transport._handle_registry[handle.order_id] == handle
        assert result.parent.raw_status == "PreSubmitted"

    @pytest.mark.parametrize(
        ("entry", "take_profit", "stop_loss"),
        [
            pytest.param(
                _hb(OrderType.STOP_LOSS, TradeType.BUY, stop="105"),
                _tp(),
                _sl(),
                id="entry-not-market-or-limit",
            ),
            pytest.param(
                _entry(),
                _hb(OrderType.MARKET, TradeType.SELL),
                _sl(),
                id="tp-not-limit",
            ),
            pytest.param(
                _entry(),
                _tp(),
                _hb(OrderType.LIMIT, TradeType.SELL, price="95"),
                id="sl-not-stop",
            ),
            pytest.param(
                _entry(),
                _hb(OrderType.LIMIT, TradeType.BUY, price="110"),
                _sl(),
                id="tp-same-side",
            ),
            pytest.param(
                _entry(),
                _tp(),
                _hb(OrderType.STOP_LOSS, TradeType.BUY, stop="95"),
                id="sl-same-side",
            ),
            pytest.param(_entry(), _tp(amount="5"), _sl(), id="tp-amount-differs"),
            pytest.param(_entry(), _tp(), _sl(amount="11"), id="sl-amount-differs"),
        ],
    )
    async def test_validation_raises_before_any_ib_call(
        self, entry: HBOrder, take_profit: HBOrder, stop_loss: HBOrder
    ) -> None:
        transport, ib = await _make()
        with pytest.raises(ValueError):  # noqa: PT011
            await transport.place_bracket(MagicMock(), entry, take_profit, stop_loss)
        assert ib.client.req_id_calls == 0
        assert ib.placed_orders == []

    async def test_not_connected_raises_and_places_nothing(self) -> None:
        transport, ib = await _make(connect=False)
        with pytest.raises(ConnectionLostError):
            await transport.place_bracket(MagicMock(), _entry(), _tp(), _sl())
        assert ib.placed_orders == []
        assert ib.client.req_id_calls == 0


class TestPlaceBracketRejection:
    async def _reject_sl(
        self, ib: MockIb, *, tp_status: str = "Submitted", code: int = 201
    ) -> None:
        await _when_placed(ib, 3)
        parent, tp, sl = ib.placed_orders
        if tp_status == "Filled":
            ib.trades_by_order_id[tp.orderId].orderStatus.filled = 10.0
        ib.emit_status(parent.orderId, "Submitted")
        ib.emit_status(tp.orderId, tp_status)
        ib.emit_error(sl.orderId, code, "order rejected")

    async def test_rejected_leg_rolls_back_siblings(self) -> None:
        transport, ib = await _make()
        results = await asyncio.gather(
            self._reject_sl(ib),
            transport.place_bracket(MagicMock(), _entry(), _tp(), _sl()),
            return_exceptions=True,
        )
        exc = results[1]
        assert isinstance(exc, _linked_error())
        assert isinstance(exc, OrderRejectedError)
        assert exc.error_code == 201
        assert _ids(exc.handles) == {FIRST_ID, FIRST_ID + 1, FIRST_ID + 2}
        assert len(exc.handles) == 3
        assert {o.orderId for o in ib.cancel_order_calls} >= {FIRST_ID, FIRST_ID + 1}
        assert not _ids(exc.cancel_errors)

    async def test_terminal_sibling_is_not_cancelled(self) -> None:
        transport, ib = await _make()
        results = await asyncio.gather(
            self._reject_sl(ib, tp_status="Filled"),
            transport.place_bracket(MagicMock(), _entry(), _tp(), _sl()),
            return_exceptions=True,
        )
        assert isinstance(results[1], _linked_error())
        cancelled = {o.orderId for o in ib.cancel_order_calls}
        assert FIRST_ID in cancelled
        assert FIRST_ID + 1 not in cancelled

    async def test_failed_rollback_cancel_is_reported(self) -> None:
        transport, ib = await _make()
        ib.cancel_errors[FIRST_ID + 1] = RuntimeError("cancel failed")
        results = await asyncio.gather(
            self._reject_sl(ib),
            transport.place_bracket(MagicMock(), _entry(), _tp(), _sl()),
            return_exceptions=True,
        )
        exc = results[1]
        assert isinstance(exc, _linked_error())
        assert exc.error_code == 201
        assert _ids(exc.cancel_errors) == {FIRST_ID + 1}
        assert FIRST_ID in {o.orderId for o in ib.cancel_order_calls}


# --------------------------------------------------------------------------- D


def _oca_orders() -> list[HBOrder]:
    return [
        _hb(OrderType.LIMIT, TradeType.SELL, price="110"),
        _hb(OrderType.STOP_LOSS, TradeType.SELL, stop="95"),
    ]


class TestPlaceOca:
    async def test_group_type_and_transmit(self) -> None:
        transport, ib = await _make()
        _, result = await asyncio.gather(
            _ack_all(ib, 2), transport.place_oca(MagicMock(), _oca_orders())
        )
        groups = {o.ocaGroup for o in ib.placed_orders}
        assert len(groups) == 1
        group = groups.pop()
        assert isinstance(group, str)
        assert group
        assert {o.ocaType for o in ib.placed_orders} == {1}
        assert [o.transmit for o in ib.placed_orders] == [True, True]
        assert result.group == group
        assert result.oca_type == _linked().OcaType.CANCEL_WITH_BLOCK
        assert isinstance(result, _linked().IbOcaGroup)

    async def test_two_calls_use_different_groups(self) -> None:
        transport, ib = await _make()
        _, first = await asyncio.gather(
            _ack_all(ib, 2), transport.place_oca(MagicMock(), _oca_orders())
        )
        _, second = await asyncio.gather(
            _ack_all_from(ib, 2, 4), transport.place_oca(MagicMock(), _oca_orders())
        )
        assert first.group != second.group
        assert {o.ocaGroup for o in ib.placed_orders[:2]} == {first.group}
        assert {o.ocaGroup for o in ib.placed_orders[2:]} == {second.group}

    async def test_orders_mapped_and_account_stamped(self) -> None:
        transport, ib = await _make()
        _, result = await asyncio.gather(
            _ack_all(ib, 2), transport.place_oca(MagicMock(), _oca_orders())
        )
        first, second = ib.placed_orders
        assert (first.orderType, first.lmtPrice, first.action) == ("LMT", 110.0, "SELL")
        assert (second.orderType, second.auxPrice, second.action) == ("STP", 95.0, "SELL")
        assert [o.account for o in ib.placed_orders] == [ACCT, ACCT]
        assert [h.order_id for h in result.orders] == [o.orderId for o in ib.placed_orders]
        assert isinstance(result.orders, tuple)
        for handle in result.orders:
            assert transport._handle_registry[handle.order_id] == handle

    async def test_non_default_oca_type_sent(self) -> None:
        transport, ib = await _make()
        oca_type = _linked().OcaType.REDUCE_NO_BLOCK
        _, result = await asyncio.gather(
            _ack_all(ib, 2), transport.place_oca(MagicMock(), _oca_orders(), oca_type)
        )
        assert {o.ocaType for o in ib.placed_orders} == {3}
        assert result.oca_type == oca_type

    async def test_returns_after_every_order_has_status(self) -> None:
        transport, ib = await _make()

        async def ack_in_reverse() -> None:
            await _when_placed(ib, 3)
            for order in reversed(ib.placed_orders):
                ib.emit_status(order.orderId, "PreSubmitted")

        orders = [*_oca_orders(), _hb(OrderType.LIMIT, TradeType.SELL, price="120")]
        _, result = await asyncio.gather(ack_in_reverse(), transport.place_oca(MagicMock(), orders))
        assert [h.raw_status for h in result.orders] == ["PreSubmitted"] * 3
        assert [h.order_id for h in result.orders] == [o.orderId for o in ib.placed_orders]

    async def test_fewer_than_two_orders_raises_before_ib_call(self) -> None:
        transport, ib = await _make()
        with pytest.raises(ValueError):  # noqa: PT011
            await transport.place_oca(MagicMock(), _oca_orders()[:1])
        assert ib.placed_orders == []
        assert ib.client.req_id_calls == 0

    async def test_not_connected_raises(self) -> None:
        transport, ib = await _make(connect=False)
        with pytest.raises(ConnectionLostError):
            await transport.place_oca(MagicMock(), _oca_orders())
        assert ib.placed_orders == []

    async def test_rejected_member_rolls_back(self) -> None:
        transport, ib = await _make()

        async def reject_second() -> None:
            await _when_placed(ib, 2)
            first, second = ib.placed_orders
            ib.emit_status(first.orderId, "Submitted")
            ib.emit_error(second.orderId, 201, "order rejected")

        results = await asyncio.gather(
            reject_second(),
            transport.place_oca(MagicMock(), _oca_orders()),
            return_exceptions=True,
        )
        exc = results[1]
        assert isinstance(exc, _linked_error())
        assert exc.error_code == 201
        assert len(exc.handles) == 2
        first_id = ib.placed_orders[0].orderId
        assert first_id in {o.orderId for o in ib.cancel_order_calls}
        assert not _ids(exc.cancel_errors)

    async def test_failed_rollback_cancel_is_reported(self) -> None:
        transport, ib = await _make()
        ib.cancel_errors[FIRST_ID] = RuntimeError("cancel failed")

        async def reject_second() -> None:
            await _when_placed(ib, 2)
            first, second = ib.placed_orders
            ib.emit_status(first.orderId, "Submitted")
            ib.emit_error(second.orderId, 201, "order rejected")

        results = await asyncio.gather(
            reject_second(),
            transport.place_oca(MagicMock(), _oca_orders()),
            return_exceptions=True,
        )
        exc = results[1]
        assert isinstance(exc, _linked_error())
        assert _ids(exc.cancel_errors) == {FIRST_ID}


# --------------------------------------------------------------------------- E


def _gateway(*, with_resolver: bool = True) -> tuple[IbGatewayGateway, MagicMock, MagicMock]:
    transport = MagicMock()
    transport.place_bracket = AsyncMock()
    transport.place_oca = AsyncMock()
    resolver = MagicMock()
    resolved = MagicMock()
    resolved.native = object()
    resolver.resolve = AsyncMock(return_value=resolved)
    gateway = IbGatewayGateway(
        transport=transport,
        signer=MagicMock(),
        contract_resolver=resolver if with_resolver else None,
    )
    return gateway, transport, resolver


class TestGatewayDelegation:
    async def test_place_bracket_resolves_once_and_delegates(self) -> None:
        gateway, transport, resolver = _gateway()
        ref = MagicMock()
        entry, tp, sl = _entry(), _tp(), _sl()
        result = await gateway.place_bracket(ref, entry, tp, sl)
        resolver.resolve.assert_awaited_once_with(ref)
        native = resolver.resolve.return_value.native
        transport.place_bracket.assert_awaited_once_with(native, entry, tp, sl)
        assert result is transport.place_bracket.return_value

    async def test_place_oca_resolves_once_and_delegates(self) -> None:
        gateway, transport, resolver = _gateway()
        ref = MagicMock()
        orders = _oca_orders()
        oca_type = _linked().OcaType.REDUCE_WITH_BLOCK
        result = await gateway.place_oca(ref, orders, oca_type=oca_type)
        resolver.resolve.assert_awaited_once_with(ref)
        call = transport.place_oca.await_args
        assert call.args[0] is resolver.resolve.return_value.native
        assert call.args[1] == orders
        assert oca_type in (*call.args[2:], *call.kwargs.values())
        assert result is transport.place_oca.return_value

    async def test_place_bracket_without_resolver_raises(self) -> None:
        gateway, transport, _ = _gateway(with_resolver=False)
        with pytest.raises(RuntimeError):
            await gateway.place_bracket(MagicMock(), _entry(), _tp(), _sl())
        transport.place_bracket.assert_not_called()

    async def test_place_oca_without_resolver_raises(self) -> None:
        gateway, transport, _ = _gateway(with_resolver=False)
        with pytest.raises(RuntimeError):
            await gateway.place_oca(MagicMock(), _oca_orders())
        transport.place_oca.assert_not_called()
