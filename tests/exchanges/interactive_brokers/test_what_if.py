"""Acceptance tests for issue #58d: IB ``what_if()`` pre-trade margin/commission check.

Red phase: the feature does not exist yet, so the not-yet-existing names are imported
inside the tests (via the helpers below) to make each test fail on its own.
"""

from __future__ import annotations

import dataclasses
import importlib
import math
import sys
import time
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_connector.exchanges.interactive_brokers.exceptions import (
    ConnectionLostError,
    IbError,
    MarketDataTimeoutError,
)
from market_connector.exchanges.interactive_brokers.ib_gateway import IbGatewayGateway
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.transport import (
    IbGatewayTransport,
    _hb_to_ib_order,
)
from market_connector.orders import HBOrder, OrderType, TradeType

from ._doubles import MockIb, make_order_state

pytest.importorskip("ib_async")

_IB_PATH = "market_connector.exchanges.interactive_brokers.transport.IB"
_PKG = "market_connector.exchanges.interactive_brokers"
ACCT = "DU1111111"
UNSET_DOUBLE = sys.float_info.max


def _result_cls() -> type:
    # Looked up lazily so collection succeeds while the module is missing (red phase).
    return importlib.import_module(f"{_PKG}.what_if").IbWhatIfResult


def _parse(**state_kw: Any) -> Any:
    return _result_cls().from_order_state(make_order_state(**state_kw))  # type: ignore[attr-defined]


def _timeout_error() -> type[Exception]:
    return getattr(importlib.import_module(f"{_PKG}.exceptions"), "WhatIfTimeoutError")  # noqa: B009


def _limit_order() -> HBOrder:
    return HBOrder(
        order_type=OrderType.LIMIT,
        side=TradeType.BUY,
        amount=Decimal(10),
        price=Decimal("150.25"),
    )


def _market_order() -> HBOrder:
    return HBOrder(order_type=OrderType.MARKET, side=TradeType.SELL, amount=Decimal(5))


async def _make(*, connect: bool = True, **spec_kw: Any) -> tuple[IbGatewayTransport, MockIb]:
    ib = MockIb()
    ib.managed_accounts = [ACCT]
    with patch(_IB_PATH, return_value=ib):
        transport = IbGatewayTransport(IbConnectionSpec(**spec_kw))
    if connect:
        await transport.connect()
    return transport, ib


# --------------------------------------------------------------------------- A


class TestIbWhatIfResultMarginStrings:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1234.56", Decimal("1234.56")),
            ("-500.25", Decimal("-500.25")),
            ("0", Decimal("0")),
        ],
    )
    def test_parses_exact_decimal(self, raw: str, expected: Decimal) -> None:
        r = _parse(
            initMarginBefore=raw,
            initMarginChange=raw,
            initMarginAfter=raw,
            maintMarginBefore=raw,
            maintMarginChange=raw,
            maintMarginAfter=raw,
            equityWithLoanBefore=raw,
            equityWithLoanChange=raw,
            equityWithLoanAfter=raw,
        )
        for name in (
            "init_margin_before",
            "init_margin_change",
            "init_margin_after",
            "maint_margin_before",
            "maint_margin_change",
            "maint_margin_after",
            "equity_with_loan_before",
            "equity_with_loan_change",
            "equity_with_loan_after",
        ):
            value = getattr(r, name)
            assert isinstance(value, Decimal)
            assert value == expected

    def test_default_state_maps_each_field_to_its_own_source(self) -> None:
        r = _parse()
        assert r.init_margin_before == Decimal("1000.00")
        assert r.init_margin_change == Decimal("250.50")
        assert r.init_margin_after == Decimal("1250.50")
        assert r.maint_margin_before == Decimal("800.00")
        assert r.maint_margin_change == Decimal("200.25")
        assert r.maint_margin_after == Decimal("1000.25")
        assert r.equity_with_loan_before == Decimal("50000.00")
        assert r.equity_with_loan_change == Decimal("-100.00")
        assert r.equity_with_loan_after == Decimal("49900.00")

    @pytest.mark.parametrize(
        "raw",
        ["", "abc", "1,234.5", str(UNSET_DOUBLE), "1.7976931348623157E308"],
    )
    def test_empty_unparsable_or_unset_is_none(self, raw: str) -> None:
        r = _parse(
            initMarginBefore=raw,
            initMarginChange=raw,
            initMarginAfter=raw,
            maintMarginBefore=raw,
            maintMarginChange=raw,
            maintMarginAfter=raw,
            equityWithLoanBefore=raw,
            equityWithLoanChange=raw,
            equityWithLoanAfter=raw,
        )
        assert r.init_margin_before is None
        assert r.init_margin_change is None
        assert r.init_margin_after is None
        assert r.maint_margin_before is None
        assert r.maint_margin_change is None
        assert r.maint_margin_after is None
        assert r.equity_with_loan_before is None
        assert r.equity_with_loan_change is None
        assert r.equity_with_loan_after is None


class TestIbWhatIfResultCommission:
    @pytest.mark.parametrize("raw", [UNSET_DOUBLE, math.nan, None])
    def test_unset_nan_or_none_is_none(self, raw: float | None) -> None:
        r = _parse(commission=raw, minCommission=raw, maxCommission=raw)
        assert r.commission is None
        assert r.min_commission is None
        assert r.max_commission is None

    def test_zero_is_real(self) -> None:
        r = _parse(commission=0.0, minCommission=0.0, maxCommission=0.0)
        assert r.commission == Decimal("0")
        assert isinstance(r.commission, Decimal)
        assert r.min_commission == Decimal("0")
        assert r.max_commission == Decimal("0")

    def test_exact_decimal_from_float(self) -> None:
        r = _parse(commission=1.25, minCommission=0.35, maxCommission=2.5)
        assert r.commission == Decimal("1.25")
        assert r.min_commission == Decimal("0.35")
        assert r.max_commission == Decimal("2.5")


class TestIbWhatIfResultText:
    def test_empty_strings_are_none(self) -> None:
        r = _parse(commissionCurrency="", warningText="")
        assert r.commission_currency is None
        assert r.warning_text is None

    def test_non_empty_strings_returned_not_raised(self) -> None:
        r = _parse(commissionCurrency="USD", warningText="Insufficient margin")
        assert r.commission_currency == "USD"
        assert r.warning_text == "Insufficient margin"

    def test_result_is_frozen(self) -> None:
        r = _parse()
        assert dataclasses.is_dataclass(r)
        with pytest.raises(dataclasses.FrozenInstanceError):
            r.commission = Decimal(1)

    def test_field_set(self) -> None:
        names = {f.name for f in dataclasses.fields(_result_cls())}  # type: ignore[arg-type]
        assert names == {
            "init_margin_before",
            "init_margin_change",
            "init_margin_after",
            "maint_margin_before",
            "maint_margin_change",
            "maint_margin_after",
            "equity_with_loan_before",
            "equity_with_loan_change",
            "equity_with_loan_after",
            "commission",
            "min_commission",
            "max_commission",
            "commission_currency",
            "warning_text",
        }


# --------------------------------------------------------------------------- B


class TestSpecWhatIfTimeout:
    def test_default_is_ten_seconds(self) -> None:
        assert IbConnectionSpec().what_if_timeout == 10.0  # type: ignore[attr-defined]

    @pytest.mark.parametrize("value", [0, 0.0, -1.0])
    def test_rejects_non_positive(self, value: float) -> None:
        with pytest.raises(ValueError, match="what_if_timeout"):
            IbConnectionSpec(what_if_timeout=value)  # type: ignore[call-arg]

    def test_accepts_small_positive(self) -> None:
        assert IbConnectionSpec(what_if_timeout=0.05).what_if_timeout == 0.05  # type: ignore[call-arg,attr-defined]


# --------------------------------------------------------------------------- C


class TestWhatIfTimeoutError:
    def test_subclasses_ib_error_with_code_zero(self) -> None:
        exc_cls = _timeout_error()
        assert issubclass(exc_cls, IbError)
        assert exc_cls(0, "no what-if answer").error_code == 0  # type: ignore[call-arg]

    def test_is_not_market_data_timeout(self) -> None:
        assert not issubclass(_timeout_error(), MarketDataTimeoutError)

    def test_has_docstring(self) -> None:
        assert _timeout_error().__doc__


# --------------------------------------------------------------------------- D


class TestTransportWhatIf:
    @pytest.mark.parametrize("make_order", [_limit_order, _market_order])
    async def test_calls_ib_once_with_place_order_equivalent_order(self, make_order: Any) -> None:
        transport, ib = await _make()
        native = MagicMock(name="native_contract")
        hb_order = make_order()
        await transport.what_if(native, hb_order)  # type: ignore[attr-defined]
        assert len(ib.what_if_calls) == 1
        contract, order = ib.what_if_calls[0]
        assert contract is native
        expected = _hb_to_ib_order(hb_order)
        assert order.orderType == expected.orderType
        assert order.action == expected.action
        assert order.totalQuantity == expected.totalQuantity
        assert order.lmtPrice == expected.lmtPrice
        assert order.tif == expected.tif

    async def test_order_carries_resolved_account(self) -> None:
        transport, ib = await _make()
        assert transport.account == ACCT
        await transport.what_if(MagicMock(), _limit_order())  # type: ignore[attr-defined]
        assert ib.what_if_calls[0][1].account == ACCT

    async def test_places_no_order_and_tracks_nothing(self) -> None:
        transport, ib = await _make()
        ib.placeOrder = MagicMock(side_effect=AssertionError("placeOrder must not be called"))
        await transport.what_if(MagicMock(), _limit_order())  # type: ignore[attr-defined]
        ib.placeOrder.assert_not_called()
        assert transport.open_orders() == []
        assert transport._handle_registry == {}

    async def test_returns_parsed_order_state(self) -> None:
        transport, ib = await _make()
        ib.what_if_result = make_order_state(
            initMarginChange="777.70", commission=3.5, warningText="careful"
        )
        result = await transport.what_if(MagicMock(), _limit_order())  # type: ignore[attr-defined]
        assert result == _result_cls().from_order_state(ib.what_if_result)  # type: ignore[attr-defined]
        assert result.init_margin_change == Decimal("777.70")
        assert result.commission == Decimal("3.5")
        assert result.warning_text == "careful"

    async def test_not_connected_raises_connection_lost(self) -> None:
        transport, ib = await _make(connect=False)
        with pytest.raises(ConnectionLostError):
            await transport.what_if(MagicMock(), _limit_order())  # type: ignore[attr-defined]
        assert ib.what_if_calls == []

    async def test_limit_without_price_raises_value_error_before_ib(self) -> None:
        transport, ib = await _make()
        bad = HBOrder(order_type=OrderType.LIMIT, side=TradeType.BUY, amount=Decimal(1))
        with pytest.raises(ValueError, match="LIMIT order requires non-None price"):
            await transport.what_if(MagicMock(), bad)  # type: ignore[attr-defined]
        assert ib.what_if_calls == []

    async def test_limit_maker_raises_not_implemented_before_ib(self) -> None:
        transport, ib = await _make()
        bad = HBOrder(
            order_type=OrderType.LIMIT_MAKER,
            side=TradeType.BUY,
            amount=Decimal(1),
            price=Decimal(10),
        )
        with pytest.raises(NotImplementedError):
            await transport.what_if(MagicMock(), bad)  # type: ignore[attr-defined]
        assert ib.what_if_calls == []

    async def test_hang_raises_what_if_timeout_error_quickly(self) -> None:
        transport, ib = await _make(what_if_timeout=0.05)  # type: ignore[arg-type]
        ib.what_if_hang = True
        started = time.monotonic()
        with pytest.raises(_timeout_error()):
            await transport.what_if(MagicMock(), _limit_order())  # type: ignore[attr-defined]
        assert time.monotonic() - started < 1.0

    async def test_ib_error_propagates_unchanged(self) -> None:
        transport, ib = await _make()
        boom = IbError(201, "Order rejected - reason: margin")
        ib.what_if_error = boom
        with pytest.raises(IbError) as ei:
            await transport.what_if(MagicMock(), _limit_order())  # type: ignore[attr-defined]
        assert ei.value is boom


# --------------------------------------------------------------------------- E


class TestGatewayWhatIf:
    async def test_resolves_then_delegates_to_transport(self) -> None:
        sentinel = object()
        transport = MagicMock()
        transport.what_if = AsyncMock(return_value=sentinel)
        resolver = MagicMock()
        resolved = MagicMock()
        resolver.resolve = AsyncMock(return_value=resolved)
        gateway = IbGatewayGateway(
            transport=transport, signer=MagicMock(), contract_resolver=resolver
        )
        ref = MagicMock()
        order = _limit_order()

        result = await gateway.what_if(ref, order)  # type: ignore[attr-defined]

        assert result is sentinel
        resolver.resolve.assert_awaited_once_with(ref)
        transport.what_if.assert_awaited_once_with(resolved.native, order)

    async def test_without_resolver_raises_runtime_error(self) -> None:
        transport = MagicMock()
        transport.what_if = AsyncMock()
        gateway = IbGatewayGateway(transport=transport, signer=MagicMock())
        with pytest.raises(RuntimeError, match="contract_resolver"):
            await gateway.what_if(MagicMock(), _limit_order())  # type: ignore[attr-defined]
        transport.what_if.assert_not_awaited()
