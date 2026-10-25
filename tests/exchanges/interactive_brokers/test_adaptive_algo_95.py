"""Acceptance tests for issue #95: opt-in IB Adaptive algo on SMART stock MARKET/LIMIT orders.

Red phase: ``order_options`` does not exist yet, so it is imported lazily inside each test
(via ``_mod()``) and every test fails on its own instead of breaking collection.
"""

from __future__ import annotations

import dataclasses
import importlib
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_connector.exchanges.interactive_brokers.ib_gateway import IbGatewayGateway
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.transport import IbGatewayTransport
from market_connector.orders import HBOrder, OrderType, TradeType

from ._doubles import MockIb

pytest.importorskip("ib_async")

from ib_async import Contract  # noqa: E402

_IB_PATH = "market_connector.exchanges.interactive_brokers.transport.IB"
_PKG = "market_connector.exchanges.interactive_brokers"
ACCT = "DU1111111"
PRIORITIES = [("PATIENT", "Patient"), ("NORMAL", "Normal"), ("URGENT", "Urgent")]


def _mod() -> Any:
    # Looked up lazily so collection succeeds while the module is missing (red phase).
    return importlib.import_module(f"{_PKG}.order_options")


def _adaptive(member: str) -> Any:
    mod = _mod()
    return mod.IbOrderOptions(adaptive_priority=getattr(mod.AdaptivePriority, member))


def _contract(sec_type: str = "STK", exchange: str = "SMART", symbol: str = "AAPL") -> Contract:
    return Contract(
        secType=sec_type, symbol=symbol, exchange=exchange, currency="USD", conId=265598
    )


def _limit() -> HBOrder:
    return HBOrder(
        order_type=OrderType.LIMIT,
        side=TradeType.BUY,
        amount=Decimal(10),
        price=Decimal("150.25"),
    )


def _market() -> HBOrder:
    return HBOrder(order_type=OrderType.MARKET, side=TradeType.SELL, amount=Decimal(5))


def _stop() -> HBOrder:
    return HBOrder(
        order_type=OrderType.STOP_LOSS,
        side=TradeType.SELL,
        amount=Decimal(5),
        stop_price=Decimal("95"),
    )


async def _make() -> tuple[IbGatewayTransport, MockIb]:
    ib = MockIb()
    ib.managed_accounts = [ACCT]
    with patch(_IB_PATH, return_value=ib):
        transport = IbGatewayTransport(IbConnectionSpec(account_id=ACCT))
    await transport.connect()
    transport._first_status_timeout = 0.5
    return transport, ib


async def _place(
    transport: IbGatewayTransport,
    ib: MockIb,
    contract: Contract,
    order: HBOrder,
    **kw: Any,
) -> Any:
    """Place an order and acknowledge it once the double has recorded it."""
    import asyncio  # noqa: PLC0415

    before = len(ib.placed_orders)

    async def _ack() -> None:
        for _ in range(500):
            if len(ib.placed_orders) > before:
                ib.emit_status(ib.placed_orders[-1].orderId, "Submitted")
                return
            await asyncio.sleep(0.002)

    _, handle = await asyncio.gather(_ack(), transport.place_order(contract, order, **kw))
    return handle


def _assert_adaptive(order: Any, wire_value: str) -> None:
    assert order.algoStrategy == "Adaptive"
    assert len(order.algoParams) == 1
    assert order.algoParams[0].tag == "adaptivePriority"
    assert order.algoParams[0].value == wire_value


def _assert_no_algo(order: Any) -> None:
    assert not order.algoStrategy
    assert not order.algoParams


# --------------------------------------------------------------------------- 1


class TestOrderOptionsTypes:
    def test_defaults_to_no_adaptive_priority(self) -> None:
        assert _mod().IbOrderOptions().adaptive_priority is None

    def test_is_frozen_dataclass(self) -> None:
        opts = _mod().IbOrderOptions()
        assert dataclasses.is_dataclass(opts)
        with pytest.raises(dataclasses.FrozenInstanceError):
            opts.adaptive_priority = _mod().AdaptivePriority.URGENT

    @pytest.mark.parametrize(("member", "wire"), PRIORITIES)
    def test_priority_string_values(self, member: str, wire: str) -> None:
        value = getattr(_mod().AdaptivePriority, member)
        assert value == wire
        assert str(value) == wire
        assert isinstance(value, str)

    def test_priority_has_exactly_three_members(self) -> None:
        assert len(list(_mod().AdaptivePriority)) == 3


# --------------------------------------------------------------------------- 2


class TestPlaceOrderAdaptive:
    @pytest.mark.parametrize(("member", "wire"), PRIORITIES)
    @pytest.mark.parametrize("make_order", [_market, _limit])
    async def test_maps_to_adaptive_algo(self, member: str, wire: str, make_order: Any) -> None:
        transport, ib = await _make()
        await _place(transport, ib, _contract(), make_order(), options=_adaptive(member))
        _assert_adaptive(ib.placed_orders[0], wire)

    @pytest.mark.parametrize("make_order", [_market, _limit])
    async def test_other_fields_unchanged_vs_no_options(self, make_order: Any) -> None:
        transport, ib = await _make()
        await _place(transport, ib, _contract(), make_order())
        await _place(transport, ib, _contract(), make_order(), options=_adaptive("URGENT"))
        plain, adaptive = ib.placed_orders
        for name in ("action", "totalQuantity", "lmtPrice", "orderType", "tif", "account"):
            assert getattr(adaptive, name) == getattr(plain, name), name
        assert ib.placed_contracts[1].secType == "STK"


# --------------------------------------------------------------------------- 3


class TestWhatIfAdaptive:
    @pytest.mark.parametrize(("member", "wire"), PRIORITIES)
    @pytest.mark.parametrize("make_order", [_market, _limit])
    async def test_what_if_order_carries_algo(
        self, member: str, wire: str, make_order: Any
    ) -> None:
        transport, ib = await _make()
        await transport.what_if(_contract(), make_order(), options=_adaptive(member))
        assert len(ib.what_if_calls) == 1
        _assert_adaptive(ib.what_if_calls[0][1], wire)
        assert ib.placed_orders == []


# --------------------------------------------------------------------------- 4


class TestNoOptionsRegression:
    @pytest.mark.parametrize("use_empty", [False, True])
    async def test_place_order_has_no_algo(self, use_empty: bool) -> None:
        transport, ib = await _make()
        kw = {"options": _mod().IbOrderOptions()} if use_empty else {}
        await _place(transport, ib, _contract(), _limit(), **kw)
        _assert_no_algo(ib.placed_orders[0])

    async def test_place_order_explicit_none_has_no_algo(self) -> None:
        transport, ib = await _make()
        _mod()  # red until the module exists
        await _place(transport, ib, _contract(), _limit(), options=None)
        _assert_no_algo(ib.placed_orders[0])

    @pytest.mark.parametrize("use_empty", [False, True])
    async def test_what_if_has_no_algo(self, use_empty: bool) -> None:
        transport, ib = await _make()
        kw = {"options": _mod().IbOrderOptions()} if use_empty else {"options": None}
        await transport.what_if(_contract(), _market(), **kw)
        _assert_no_algo(ib.what_if_calls[0][1])

    async def test_adaptive_on_non_smart_contract_ok_when_not_requested(self) -> None:
        transport, ib = await _make()
        _mod()  # red until the module exists
        await transport.what_if(_contract("FUT", "CME", "MES"), _limit(), options=None)
        _assert_no_algo(ib.what_if_calls[0][1])


# --------------------------------------------------------------------------- 5

REJECTED = [
    pytest.param(lambda: _contract("CRYPTO", "PAXOS", "BTC"), _market, id="crypto-paxos"),
    pytest.param(lambda: _contract("FUT", "CME", "MES"), _limit, id="future"),
    pytest.param(lambda: _contract("STK", "NASDAQ"), _limit, id="stk-direct-routed"),
    pytest.param(lambda: _contract(), _stop, id="stop-loss-order"),
]


class TestAdaptiveRejections:
    @pytest.mark.parametrize(("make_contract", "make_order"), REJECTED)
    async def test_place_order_value_error_before_ib(
        self, make_contract: Any, make_order: Any
    ) -> None:
        transport, ib = await _make()
        options = _adaptive("NORMAL")
        with pytest.raises(ValueError):  # noqa: PT011
            await transport.place_order(make_contract(), make_order(), options=options)
        assert ib.placed_orders == []
        assert ib.placed_contracts == []
        assert transport._handle_registry == {}

    @pytest.mark.parametrize(("make_contract", "make_order"), REJECTED)
    async def test_what_if_value_error_before_ib(self, make_contract: Any, make_order: Any) -> None:
        transport, ib = await _make()
        options = _adaptive("NORMAL")
        with pytest.raises(ValueError):  # noqa: PT011
            await transport.what_if(make_contract(), make_order(), options=options)
        assert ib.what_if_calls == []


# --------------------------------------------------------------------------- 6


class TestGatewayForwarding:
    @staticmethod
    def _gateway() -> tuple[IbGatewayGateway, MagicMock, MagicMock]:
        transport = MagicMock()
        transport.place_order = AsyncMock(return_value=object())
        transport.what_if = AsyncMock(return_value=object())
        resolved = MagicMock()
        resolver = MagicMock()
        resolver.resolve = AsyncMock(return_value=resolved)
        gateway = IbGatewayGateway(
            transport=transport, signer=MagicMock(), contract_resolver=resolver
        )
        return gateway, transport, resolved

    async def test_place_order_forwards_options(self) -> None:
        gateway, transport, resolved = self._gateway()
        ref, order, options = MagicMock(), _limit(), _adaptive("PATIENT")
        result = await gateway.place_order(ref, order, options=options)
        assert result is transport.place_order.return_value
        transport.place_order.assert_awaited_once_with(resolved.native, order, options=options)

    async def test_what_if_forwards_options(self) -> None:
        gateway, transport, resolved = self._gateway()
        ref, order, options = MagicMock(), _market(), _adaptive("URGENT")
        result = await gateway.what_if(ref, order, options=options)
        assert result is transport.what_if.return_value
        transport.what_if.assert_awaited_once_with(resolved.native, order, options=options)
