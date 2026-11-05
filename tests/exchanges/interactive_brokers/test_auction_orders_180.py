"""Acceptance tests for issue #180: IB auction orders (MOO/LOO via TIF OPG, MOC/LOC via option).

Red phase: ``TimeInForce.OPG`` and ``IbOrderOptions.auction`` do not exist yet, so both are
referenced lazily inside the tests and each test fails on its own instead of breaking collection.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pytest

from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.transport import IbGatewayTransport
from market_connector.orders import HBOrder, OrderType, TradeType

from ._doubles import MockIb

pytest.importorskip("ib_async")

from ib_async import Contract  # noqa: E402

_IB_PATH = "market_connector.exchanges.interactive_brokers.transport.IB"
_PKG = "market_connector.exchanges.interactive_brokers"
ACCT = "DU1111111"


def _opts(**kw: Any) -> Any:
    from market_connector.exchanges.interactive_brokers.order_options import (  # noqa: PLC0415
        IbOrderOptions,
    )

    return IbOrderOptions(**kw)


def _adaptive_opts(**kw: Any) -> Any:
    from market_connector.exchanges.interactive_brokers.order_options import (  # noqa: PLC0415
        AdaptivePriority,
    )

    return _opts(adaptive_priority=AdaptivePriority.NORMAL, **kw)


def _contract(sec_type: str = "STK", exchange: str = "SMART", symbol: str = "AAPL") -> Contract:
    return Contract(
        secType=sec_type, symbol=symbol, exchange=exchange, currency="USD", conId=265598
    )


def _order(order_type: OrderType, **extra: Any) -> HBOrder:
    kw: dict[str, Any] = {}
    if order_type in (OrderType.LIMIT, OrderType.STOP_LOSS_LIMIT):
        kw["price"] = Decimal("150.25")
    if order_type in (OrderType.STOP_LOSS, OrderType.STOP_LOSS_LIMIT):
        kw["stop_price"] = Decimal("95")
    return HBOrder(order_type=order_type, side=TradeType.BUY, amount=Decimal(10), **kw, **extra)


def _opg(order_type: OrderType, **extra: Any) -> HBOrder:
    return _order(order_type, time_in_force="OPG", **extra)


async def _make() -> tuple[IbGatewayTransport, MockIb]:
    ib = MockIb()
    ib.managed_accounts = [ACCT]
    with patch(_IB_PATH, return_value=ib):
        transport = IbGatewayTransport(IbConnectionSpec(account_id=ACCT))
    await transport.connect()
    transport._first_status_timeout = 0.5
    return transport, ib


async def _place(
    transport: IbGatewayTransport, ib: MockIb, contract: Contract, order: HBOrder, **kw: Any
) -> Any:
    before = len(ib.placed_orders)

    async def _ack() -> None:
        for _ in range(500):
            if len(ib.placed_orders) > before:
                ib.emit_status(ib.placed_orders[-1].orderId, "Submitted")
                return
            await asyncio.sleep(0.002)

    _, handle = await asyncio.gather(_ack(), transport.place_order(contract, order, **kw))
    return handle


async def _assert_rejected(contract: Contract, order: Any, options: Any) -> None:
    """Both place_order and what_if raise ValueError before any IB call."""
    transport, ib = await _make()
    with pytest.raises(ValueError):  # noqa: PT011
        await transport.place_order(contract, order, options=options)
    with pytest.raises(ValueError):  # noqa: PT011
        await transport.what_if(contract, order, options=options)
    assert ib.placed_orders == []
    assert ib.placed_contracts == []
    assert ib.what_if_calls == []


# --------------------------------------------------------------------------- 1


class TestOpgTimeInForce:
    def test_opg_member(self) -> None:
        from market_connector.orders import TimeInForce  # noqa: PLC0415

        assert TimeInForce.OPG == "OPG"
        assert (
            HBOrder(
                order_type=OrderType.MARKET,
                side=TradeType.BUY,
                amount=Decimal(1),
                time_in_force="OPG",
            ).time_in_force
            is TimeInForce.OPG
        )

    async def test_moo_market(self) -> None:
        transport, ib = await _make()
        await _place(transport, ib, _contract(), _opg(OrderType.MARKET))
        placed = ib.placed_orders[0]
        assert (placed.orderType, placed.tif) == ("MKT", "OPG")

    async def test_loo_limit(self) -> None:
        transport, ib = await _make()
        await _place(transport, ib, _contract(), _opg(OrderType.LIMIT))
        placed = ib.placed_orders[0]
        assert (placed.orderType, placed.tif, placed.lmtPrice) == ("LMT", "OPG", 150.25)

    @pytest.mark.parametrize("order_type", [OrderType.MARKET, OrderType.LIMIT])
    async def test_what_if_opg(self, order_type: OrderType) -> None:
        transport, ib = await _make()
        await transport.what_if(_contract(), _opg(order_type))
        assert ib.what_if_calls[0][1].tif == "OPG"
        assert ib.placed_orders == []


# --------------------------------------------------------------------------- 2


class TestCloseAuction:
    async def test_moc_market(self) -> None:
        transport, ib = await _make()
        await _place(
            transport, ib, _contract(), _order(OrderType.MARKET), options=_opts(auction="close")
        )
        assert ib.placed_orders[0].orderType == "MOC"

    async def test_loc_limit(self) -> None:
        transport, ib = await _make()
        await _place(
            transport, ib, _contract(), _order(OrderType.LIMIT), options=_opts(auction="close")
        )
        placed = ib.placed_orders[0]
        assert (placed.orderType, placed.lmtPrice) == ("LOC", 150.25)

    @pytest.mark.parametrize(
        ("order_type", "wire"), [(OrderType.MARKET, "MOC"), (OrderType.LIMIT, "LOC")]
    )
    async def test_what_if_close(self, order_type: OrderType, wire: str) -> None:
        transport, ib = await _make()
        await transport.what_if(_contract(), _order(order_type), options=_opts(auction="close"))
        assert ib.what_if_calls[0][1].orderType == wire
        assert ib.placed_orders == []

    def test_auction_defaults_to_none(self) -> None:
        assert _opts().auction is None


# --------------------------------------------------------------------------- 3


class TestValidation:
    @pytest.mark.parametrize("sec_type", ["FUT", "CRYPTO"])
    async def test_close_auction_on_non_stk(self, sec_type: str) -> None:
        await _assert_rejected(
            _contract(sec_type, "CME", "MES"), _order(OrderType.LIMIT), _opts(auction="close")
        )

    @pytest.mark.parametrize("sec_type", ["FUT", "CRYPTO"])
    async def test_opg_on_non_stk(self, sec_type: str) -> None:
        await _assert_rejected(_contract(sec_type, "CME", "MES"), _opg(OrderType.LIMIT), None)

    @pytest.mark.parametrize("order_type", [OrderType.STOP_LOSS, OrderType.STOP_LOSS_LIMIT])
    async def test_close_auction_with_unsupported_order_type(self, order_type: OrderType) -> None:
        await _assert_rejected(_contract(), _order(order_type), _opts(auction="close"))

    @pytest.mark.parametrize("order_type", [OrderType.STOP_LOSS, OrderType.STOP_LOSS_LIMIT])
    async def test_opg_with_unsupported_order_type(self, order_type: OrderType) -> None:
        await _assert_rejected(_contract(), _opg(order_type), None)

    async def test_loc_without_price(self) -> None:
        # HBOrder may itself refuse a priceless LIMIT; either way nothing reaches IB.
        transport, ib = await _make()
        with pytest.raises((ValueError, TypeError)):
            order = HBOrder(order_type=OrderType.LIMIT, side=TradeType.BUY, amount=Decimal(10))
            await transport.place_order(_contract(), order, options=_opts(auction="close"))
        assert ib.placed_orders == []

    async def test_outside_rth_with_close_auction(self) -> None:
        order = _order(OrderType.MARKET, outside_rth=True)
        await _assert_rejected(_contract(), order, _opts(auction="close"))

    async def test_outside_rth_with_opg(self) -> None:
        await _assert_rejected(_contract(), _opg(OrderType.MARKET, outside_rth=True), None)

    async def test_opg_with_close_auction(self) -> None:
        await _assert_rejected(_contract(), _opg(OrderType.LIMIT), _opts(auction="close"))

    @pytest.mark.parametrize("order_type", [OrderType.MARKET, OrderType.LIMIT])
    async def test_auction_with_adaptive(self, order_type: OrderType) -> None:
        await _assert_rejected(_contract(), _order(order_type), _adaptive_opts(auction="close"))


# --------------------------------------------------------------------------- 4


class TestNoLocalCutoff:
    @pytest.mark.parametrize("hour", [3, 9, 15, 16, 23])
    async def test_close_auction_accepted_at_any_time(self, hour: int) -> None:
        # The connector applies no wall-clock cut-off; IB owns timing. Whatever the clock says,
        # the order is submitted.
        transport, ib = await _make()
        with patch("time.time", return_value=1_700_000_000 + hour * 3600):
            await _place(
                transport,
                ib,
                _contract(),
                _order(OrderType.LIMIT),
                options=_opts(auction="close"),
            )
        assert ib.placed_orders[0].orderType == "LOC"

    async def test_opg_accepted_mid_session(self) -> None:
        transport, ib = await _make()
        with patch("time.time", return_value=1_700_000_000 + 15 * 3600):
            await _place(transport, ib, _contract(), _opg(OrderType.MARKET))
        assert ib.placed_orders[0].tif == "OPG"


# --------------------------------------------------------------------------- 5


class TestRegression:
    @pytest.mark.parametrize("tif", ["DAY", "GTC", "IOC"])
    @pytest.mark.parametrize(
        ("order_type", "wire"), [(OrderType.MARKET, "MKT"), (OrderType.LIMIT, "LMT")]
    )
    async def test_existing_tifs_unchanged(
        self, tif: str, order_type: OrderType, wire: str
    ) -> None:
        transport, ib = await _make()
        await _place(transport, ib, _contract(), _order(order_type, time_in_force=tif))
        placed = ib.placed_orders[0]
        assert (placed.orderType, placed.tif) == (wire, tif)

    async def test_adaptive_still_works_without_auction(self) -> None:
        transport, ib = await _make()
        from market_connector.exchanges.interactive_brokers.order_options import (  # noqa: PLC0415
            AdaptivePriority,
            IbOrderOptions,
        )

        opts = IbOrderOptions(adaptive_priority=AdaptivePriority.URGENT)
        await _place(transport, ib, _contract(), _order(OrderType.LIMIT), options=opts)
        placed = ib.placed_orders[0]
        assert placed.orderType == "LMT"
        assert placed.algoStrategy == "Adaptive"

    async def test_outside_rth_still_allowed_without_auction(self) -> None:
        transport, ib = await _make()
        await _place(transport, ib, _contract(), _order(OrderType.LIMIT, outside_rth=True))
        assert ib.placed_orders[0].outsideRth is True
