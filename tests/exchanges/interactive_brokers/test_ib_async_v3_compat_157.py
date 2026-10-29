"""Acceptance tests for issue #157: the IB adapter must run on ib_async 2.1.x AND 3.0.

ib_async 3.0 (branch ``next-protobuf``) changed behaviour the adapter relied on. Each test pins
the v3 behaviour explicitly so it is meaningful under the installed (2.1) library too:

- ``Wrapper.ticker2ReqId`` / ``Wrapper.reqId2Ticker`` are gone: reqId error routing for trades, L1
  and depth streams must not depend on them.
- ``OrderStatus.filled/avgFillPrice/remaining`` default to ``None`` (unset), not 0.0.
- ``PnLSingle.position`` defaults to ``None``.
- ``OrderState.commission`` became ``commissionAndFees``.
- ``Order.totalQuantity/lmtPrice/auxPrice`` are ``Decimal | None``: no float rounding on the way in.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from market_connector.exchanges.interactive_brokers.exceptions import (
    MarketDataLinesExhaustedError,
)
from market_connector.exchanges.interactive_brokers.ib_gateway import IbGatewayGateway
from market_connector.exchanges.interactive_brokers.order_handle import IBOrderState, OrderHandle
from market_connector.exchanges.interactive_brokers.pnl import IbPositionPnL
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.transport import (
    IbGatewayTransport,
    _hb_to_ib_order,
)
from market_connector.exchanges.interactive_brokers.what_if import IbWhatIfResult
from market_connector.orders import HBOrder, OrderType, TradeType

from ._doubles import MockIb, make_order_state
from .test_bracket_oca import _ack_all, _make
from .test_bracket_oca import _hb as _oca_hb
from .test_market_data_real_contract import _resolved
from .test_persistent_market_data import FakeContract, FakeResolver, Rig, _set_l1
from .test_pnl_stream_98 import ACCT, CON_ID
from .test_pnl_stream_98 import _make as _make_pnl

pytest.importorskip("ib_async")

_IB_PATH = "market_connector.exchanges.interactive_brokers.transport.IB"
_PROMPT = 2.0
_MSG_101 = "Max number of tickers has been reached"

# More significant digits than a float carries: any float round-trip changes the value.
_PRICE = Decimal("123.456789012345678")
_STOP = Decimal("98.765432109876543")
_QTY = Decimal("10.123456789012345678")


class V3Ib(MockIb):
    """MockIb whose Wrapper has no ticker2ReqId/reqId2Ticker, as in ib_async 3.0.

    The reqId is still the one the client hands out for the wire request (client.getReqId()),
    and is recorded in ``_tick_req_ids`` / ``_mkt_data_req_ids`` / ``_depth_req_ids``.
    """

    def __init__(self) -> None:
        super().__init__()
        self.wrapper = SimpleNamespace()

    def _start_ticker(self, req_type: str, ticker: Any) -> int:
        return self.client.getReqId()


async def _v3_rig(**spec_kwargs: Any) -> Rig:
    spec_kwargs.setdefault("reconnect_initial_delay", 0.01)
    spec_kwargs.setdefault("reconnect_max_delay", 0.01)
    spec_kwargs.setdefault("market_data_timeout", 30.0)
    ib = V3Ib()
    with patch(_IB_PATH, return_value=ib):
        transport = IbGatewayTransport(IbConnectionSpec(**spec_kwargs))
    resolver = FakeResolver()
    gateway = IbGatewayGateway(transport=transport, signer=MagicMock(), contract_resolver=resolver)
    rig = Rig(gateway, transport, ib, resolver)
    await rig.gateway.start()
    return rig


def _noop(_data: Any) -> None:
    return None


async def _wait_until(predicate: Any) -> None:
    for _ in range(int(_PROMPT / 0.005)):
        if predicate():
            return
        await asyncio.sleep(0.005)


async def _request_id(request_ids: list[int]) -> int:
    for _ in range(500):
        await asyncio.sleep(0.002)
        if request_ids:
            return request_ids[0]
    raise AssertionError("connector never sent the market data request")


# --------------------------------------------------------------------------- 1. reqId routing


def test_v3_double_has_no_ticker_registries() -> None:
    wrapper = V3Ib().wrapper
    assert not hasattr(wrapper, "ticker2ReqId")
    assert not hasattr(wrapper, "reqId2Ticker")


async def test_trades_stream_error_101_routes_to_subscription_without_ticker_registry() -> None:
    rig = await _v3_rig(market_data_lines=1)
    errors: list[Exception] = []
    handle = rig.transport.subscribe("trades", _resolved(1), _noop, on_error=errors.append)
    assert rig.ib._tick_req_ids, "reqTickByTickData was not sent"

    rig.ib.errorEvent(rig.ib._tick_req_ids[0], 101, _MSG_101, None)
    await _wait_until(lambda: handle.error is not None)

    assert isinstance(handle.error, MarketDataLinesExhaustedError), repr(handle.error)
    assert handle.closed is True
    assert errors == [handle.error]
    assert rig.transport.market_data_lines_in_use == 0
    assert len(rig.ib._cancel_tick_by_tick_calls) == 1


async def test_trades_stream_error_on_unknown_reqid_is_ignored_without_ticker_registry() -> None:
    rig = await _v3_rig(market_data_lines=1)
    handle = rig.transport.subscribe("trades", _resolved(1), _noop)

    rig.ib.errorEvent(rig.ib._tick_req_ids[0] + 5000, 101, _MSG_101, None)
    await asyncio.sleep(0.05)

    assert handle.error is None
    assert handle.closed is False
    assert rig.transport.market_data_lines_in_use == 1


async def test_depth_stream_error_101_fails_every_subscriber_without_ticker_registry() -> None:
    rig = await _v3_rig(market_data_lines=1)
    contract = FakeContract(1)
    errors_a: list[Exception] = []
    errors_b: list[Exception] = []
    handle_a = rig.transport.subscribe("depth", contract, _noop, on_error=errors_a.append)
    handle_b = rig.transport.subscribe("depth", contract, _noop, on_error=errors_b.append)
    assert len(rig.ib._depth_req_ids) == 1

    rig.ib.errorEvent(rig.ib._depth_req_ids[0], 101, _MSG_101, None)
    await _wait_until(lambda: handle_a.closed and handle_b.closed)

    assert isinstance(handle_a.error, MarketDataLinesExhaustedError), repr(handle_a.error)
    assert isinstance(handle_b.error, MarketDataLinesExhaustedError), repr(handle_b.error)
    assert errors_a == [handle_a.error]
    assert errors_b == [handle_b.error]
    assert rig.transport.market_data_lines_in_use == 0
    assert len(rig.ib._cancel_mkt_depth_calls) == 1


async def test_l1_stream_error_101_fails_top_of_book_without_ticker_registry() -> None:
    rig = await _v3_rig(market_data_lines=1)
    _set_l1(rig.ib._l1_ticker, float("nan"), float("nan"))  # no quote yet: the call waits
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
    assert rig.transport.market_data_lines_in_use == 0


async def test_resubscribe_all_reissues_streams_without_ticker_registry() -> None:
    rig = await _v3_rig(market_data_lines=3)
    handle = rig.transport.subscribe("trades", _resolved(1), _noop)
    rig.transport.subscribe("depth", FakeContract(2), _noop)

    rig.transport.resubscribe_all(cancel_old=False)

    assert len(rig.ib.req_tick_by_tick_calls) == 2
    assert len(rig.ib.req_mkt_depth_calls) == 2
    assert rig.transport.market_data_lines_in_use == 2
    # the re-issued request carries a new reqId; an error on it still reaches the handle
    rig.ib.errorEvent(rig.ib._tick_req_ids[-1], 101, _MSG_101, None)
    await _wait_until(lambda: handle.error is not None)
    assert isinstance(handle.error, MarketDataLinesExhaustedError), repr(handle.error)


# --------------------------------------------------------------------------- 2. OrderStatus None


def _trade_with_status(status: str, **status_fields: Any) -> Any:
    from ib_async import Contract, Order, OrderStatus, Trade  # noqa: PLC0415

    order = Order(orderId=7, permId=4242, action="BUY", orderType="LMT")
    return Trade(
        contract=Contract(conId=1),
        order=order,
        orderStatus=OrderStatus(orderId=7, status=status, **status_fields),
    )


def test_order_handle_treats_unset_fill_fields_as_nothing_filled() -> None:
    trade = _trade_with_status("Submitted", filled=None, avgFillPrice=None, remaining=None)

    handle = OrderHandle.from_trade(trade)

    assert handle.status is IBOrderState.SUBMITTED
    assert handle.filled_qty == Decimal(0)
    assert handle.avg_fill_price is None


def test_order_handle_set_fill_fields_are_unchanged() -> None:
    trade = _trade_with_status("Submitted", filled=4.0, avgFillPrice=101.5, remaining=6.0)

    handle = OrderHandle.from_trade(trade)

    assert handle.status is IBOrderState.PARTIALLY_FILLED
    assert handle.filled_qty == Decimal("4.0")
    assert handle.avg_fill_price == Decimal("101.5")


# --------------------------------------------------------------------------- 3. PnLSingle None


def test_position_pnl_with_unset_position_reads_as_flat() -> None:
    from ib_async import PnLSingle  # noqa: PLC0415

    pnl = PnLSingle(ACCT, "", CON_ID)
    pnl.position = None  # the ib_async 3.0 default

    result = IbPositionPnL.from_pnl_single(pnl)

    assert result.position == Decimal(0)
    assert result.con_id == CON_ID


async def test_position_pnl_stream_survives_unset_position() -> None:
    transport, ib = await _make_pnl()
    ib.pnl_single_values = {**ib.pnl_single_values, "position": None}
    got: list[IbPositionPnL] = []

    transport.subscribe_position_pnl(CON_ID, got.append)  # type: ignore[attr-defined]
    await _wait_until(lambda: bool(got))

    assert [item.position for item in got] == [Decimal(0)]
    assert got[0].daily_pnl == Decimal("1.5")


# --------------------------------------------------------------------------- 4. OrderState rename


def test_what_if_commission_is_read_from_the_installed_orderstate_field() -> None:
    result = IbWhatIfResult.from_order_state(make_order_state())

    assert result.commission == Decimal("1.25")


# --------------------------------------------------------------------------- 5. Order Decimals


def _hb(order_type: OrderType, **fields: Any) -> HBOrder:
    return HBOrder(order_type=order_type, side=TradeType.BUY, amount=_QTY, **fields)


def _exact(value: Any) -> Decimal:
    """The value as a Decimal, exactly as held (a float is read via repr, i.e. as it prints)."""
    return Decimal(str(value))


def test_limit_order_carries_exact_decimal_quantity_and_price() -> None:
    order = _hb_to_ib_order(_hb(OrderType.LIMIT, price=_PRICE))

    assert _exact(order.totalQuantity) == _QTY
    assert _exact(order.lmtPrice) == _PRICE


def test_stop_order_carries_exact_decimal_quantity_and_stop_price() -> None:
    order = _hb_to_ib_order(_hb(OrderType.STOP_LOSS, stop_price=_STOP))

    assert _exact(order.totalQuantity) == _QTY
    assert _exact(order.auxPrice) == _STOP


def test_stop_limit_order_carries_exact_decimal_prices() -> None:
    order = _hb_to_ib_order(_hb(OrderType.STOP_LOSS_LIMIT, price=_PRICE, stop_price=_STOP))

    assert _exact(order.totalQuantity) == _QTY
    assert _exact(order.lmtPrice) == _PRICE
    assert _exact(order.auxPrice) == _STOP


def test_market_order_carries_exact_decimal_quantity() -> None:
    order = _hb_to_ib_order(_hb(OrderType.MARKET))

    assert _exact(order.totalQuantity) == _QTY


async def test_bracket_legs_carry_exact_decimal_values() -> None:
    transport, ib = await _make()
    qty = str(_QTY)
    entry = _oca_hb(OrderType.LIMIT, TradeType.BUY, price=str(_PRICE), amount=qty)
    take_profit = _oca_hb(OrderType.LIMIT, TradeType.SELL, price=str(_PRICE + 1), amount=qty)
    stop = _oca_hb(OrderType.STOP_LOSS, TradeType.SELL, stop=str(_STOP), amount=qty)

    await asyncio.gather(
        _ack_all(ib, 3), transport.place_bracket(MagicMock(), entry, take_profit, stop)
    )

    parent, tp, sl = ib.placed_orders
    assert _exact(parent.lmtPrice) == _PRICE
    assert _exact(tp.lmtPrice) == _PRICE + 1
    assert _exact(sl.auxPrice) == _STOP
    for order in ib.placed_orders:
        assert _exact(order.totalQuantity) == _QTY
