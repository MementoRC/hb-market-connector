"""Acceptance tests for issue #181: get_account_summary and get_fills (fill history).

Red phase: new names are resolved lazily so each test fails on its own.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_connector.exchanges.interactive_brokers.ib_gateway import IbGatewayGateway
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.transport import (
    FeeEvent,
    FillEvent,
    IbGatewayTransport,
)
from market_connector.orders import TradeType

from ._doubles import MockIb, make_commission_report, make_fill

pytest.importorskip("ib_async")

_IB_PATH = "market_connector.exchanges.interactive_brokers.transport.IB"
_TRANSPORT = "market_connector.exchanges.interactive_brokers.transport"
ACCT = "DU1111111"
OTHER = "DU2222222"
UNSET_DOUBLE = 1.7976931348623157e308
UTC = timezone.utc  # noqa: UP017
SINCE = datetime(2026, 1, 2, 9, 5, 7, tzinfo=UTC)


def _summary_cls() -> type:
    return importlib.import_module(_TRANSPORT).IbAccountSummary


def _av(tag: str, value: Any, currency: str, account: str = ACCT) -> Any:
    from ib_async import AccountValue  # noqa: PLC0415

    return AccountValue(account, tag, value, currency, "")


def _make(account_id: str | None = ACCT) -> tuple[IbGatewayTransport, MockIb]:
    ib = MockIb()
    ib.managed_accounts = [ACCT]
    with patch(_IB_PATH, return_value=ib):
        transport = IbGatewayTransport(IbConnectionSpec(account_id=account_id))

    async def fake_sleep(_delay: float) -> None:
        await asyncio.sleep(0)

    transport._sleep = fake_sleep
    return transport, ib


def _rows() -> list[Any]:
    return [
        _av("NetLiquidation", "10500.75", "BASE"),
        _av("NetLiquidation", "10500.75", "USD"),
        _av("NetLiquidation", "250.10", "EUR"),
        _av("GrossPositionValue", "4000.5", "BASE"),
        _av("GrossPositionValue", "3000.25", "USD"),
        _av("TotalCashValue", "6500.25", "BASE"),
        _av("TotalCashValue", "6000.00", "USD"),
        _av("TotalCashValue", "250.10", "EUR"),
        _av("NetLiquidation", "999", "BASE", OTHER),
        _av("AvailableFunds", "1000.50", "USD"),
    ]


class TestAccountSummary:
    async def test_default_uses_base_rows(self) -> None:
        transport, ib = _make()
        ib.account_value_rows = _rows()
        await transport.connect()
        summary = await transport.get_account_summary()
        assert summary == _summary_cls()(
            account=ACCT,
            currency="BASE",
            net_liquidation=Decimal("10500.75"),
            gross_position_value=Decimal("4000.5"),
            total_cash_value=Decimal("6500.25"),
        )

    async def test_named_currency(self) -> None:
        transport, ib = _make()
        ib.account_value_rows = _rows()
        await transport.connect()
        summary = await transport.get_account_summary("eur")
        assert summary.currency == "EUR"
        assert summary.net_liquidation == Decimal("250.10")
        assert summary.total_cash_value == Decimal("250.10")
        assert summary.gross_position_value is None

    async def test_other_accounts_rows_ignored(self) -> None:
        transport, ib = _make()
        ib.account_value_rows = [_av("NetLiquidation", "999", "BASE", OTHER)]
        await transport.connect()
        summary = await transport.get_account_summary()
        assert summary.net_liquidation is None
        assert summary.account == ACCT

    async def test_missing_tags_are_none_not_zero(self) -> None:
        transport, ib = _make()
        ib.account_value_rows = [_av("NetLiquidation", "5", "BASE")]
        await transport.connect()
        summary = await transport.get_account_summary()
        assert summary.net_liquidation == Decimal(5)
        assert summary.gross_position_value is None
        assert summary.total_cash_value is None

    @pytest.mark.parametrize(
        "raw", [None, "", "nan", float("nan"), UNSET_DOUBLE, str(UNSET_DOUBLE)]
    )
    async def test_unset_values_are_none(self, raw: Any) -> None:
        transport, ib = _make()
        ib.account_value_rows = [_av("NetLiquidation", raw, "BASE")]
        await transport.connect()
        summary = await transport.get_account_summary()
        assert summary.net_liquidation is None

    async def test_exact_decimal_no_float_round_trip(self) -> None:
        transport, ib = _make()
        ib.account_value_rows = [_av("NetLiquidation", "0.1000000000000000055511", "BASE")]
        await transport.connect()
        summary = await transport.get_account_summary()
        assert summary.net_liquidation == Decimal("0.1000000000000000055511")
        assert isinstance(summary.net_liquidation, Decimal)

    async def test_summary_is_frozen_dataclass(self) -> None:
        cls = _summary_cls()
        assert dataclasses.is_dataclass(cls)
        assert [f.name for f in dataclasses.fields(cls)] == [
            "account",
            "currency",
            "net_liquidation",
            "gross_position_value",
            "total_cash_value",
        ]
        transport, ib = _make()
        ib.account_value_rows = _rows()
        await transport.connect()
        summary = await transport.get_account_summary()
        with pytest.raises(dataclasses.FrozenInstanceError):
            summary.currency = "X"  # type: ignore[misc]

    async def test_gateway_delegates(self) -> None:
        transport = MagicMock()
        sentinel = object()
        transport.get_account_summary = AsyncMock(return_value=sentinel)
        gateway = IbGatewayGateway(transport=transport, signer=MagicMock())
        assert await gateway.get_account_summary("USD") is sentinel
        transport.get_account_summary.assert_awaited_once_with("USD")

    async def test_gateway_delegates_default_currency(self) -> None:
        transport = MagicMock()
        transport.get_account_summary = AsyncMock(return_value=object())
        gateway = IbGatewayGateway(transport=transport, signer=MagicMock())
        await gateway.get_account_summary()
        transport.get_account_summary.assert_awaited_once_with(None)


class TestGetBalanceRegression:
    async def test_available_funds_preferred(self) -> None:
        transport, ib = _make()
        ib.account_value_rows = _rows()
        await transport.connect()
        assert await transport.get_balance("USD") == Decimal("1000.50")

    async def test_falls_back_to_total_cash_value(self) -> None:
        transport, ib = _make()
        ib.account_value_rows = _rows()
        await transport.connect()
        assert await transport.get_balance("EUR") == Decimal("250.10")

    async def test_missing_currency_is_zero(self) -> None:
        transport, ib = _make()
        ib.account_value_rows = _rows()
        await transport.connect()
        assert await transport.get_balance("JPY") == Decimal(0)


def _fill_at(exec_id: str, when: datetime, **kw: Any) -> Any:
    fill = make_fill(exec_id, **kw)
    fill.time = when
    return fill


class TestGetFills:
    async def test_filter_time_client_and_account(self) -> None:
        transport, ib = _make()
        await transport.connect()
        ib.req_executions_filters.clear()  # drop the connect-time reconcile call
        await transport.get_fills(SINCE)
        (flt,) = ib.req_executions_filters
        assert flt.time == "20260102-09:05:07"
        assert flt.acctCode == ACCT
        assert flt.clientId == transport._spec.client_id

    async def test_aware_non_utc_since_converted_to_utc(self) -> None:
        transport, ib = _make()
        await transport.connect()
        ib.req_executions_filters.clear()
        tz = timezone(timedelta(hours=2))
        await transport.get_fills(datetime(2026, 1, 2, 11, 5, 7, tzinfo=tz))
        assert ib.req_executions_filters[0].time == "20260102-09:05:07"

    async def test_naive_since_raises_value_error(self) -> None:
        transport, ib = _make()
        await transport.connect()
        ib.req_executions_filters.clear()
        with pytest.raises(ValueError, match="aware|timezone|tz"):
            await transport.get_fills(datetime(2026, 1, 2, 9, 5, 7))  # noqa: DTZ001
        assert ib.req_executions_filters == []

    async def test_empty_result(self) -> None:
        transport, ib = _make()
        await transport.connect()
        assert await transport.get_fills(SINCE) == []

    async def test_fill_fields_match_live_event(self) -> None:
        transport, ib = _make()
        live: list[FillEvent] = []
        transport.subscribe_fills(live.append)
        await transport.connect()
        fill = make_fill("e1.01", price=0.1, shares=0.3, cum_qty=0.3)
        ib.execDetailsEvent(None, fill)
        ib.executions_result = [fill]
        ((got, fee),) = await transport.get_fills(SINCE)
        assert fee is None
        assert got == live[0]
        assert isinstance(got, FillEvent)
        assert got.reconciled is False
        assert got.is_correction is False
        assert got.side is TradeType.BUY
        assert got.price == Decimal("0.1")

    async def test_commission_pairs_fee_2x_attribute(self) -> None:
        transport, ib = _make()
        await transport.connect()
        report = make_commission_report("e1.01", commission=1.05, currency="USD")
        ib.executions_result = [make_fill("e1.01", report=report)]
        ((fill, fee),) = await transport.get_fills(SINCE)
        assert fill.exec_id == "e1.01"
        assert fee == FeeEvent("e1.01", 11, Decimal("1.05"), "USD")

    async def test_commission_pairs_fee_3x_attribute(self) -> None:
        transport, ib = _make()
        await transport.connect()
        fill = make_fill("e1.01")
        del fill.commissionReport
        fill.commissionAndFeesReport = make_commission_report("e1.01", 2.5, "EUR")
        ib.executions_result = [fill]
        ((_, fee),) = await transport.get_fills(SINCE)
        assert fee == FeeEvent("e1.01", 11, Decimal("2.5"), "EUR")

    async def test_empty_report_exec_id_gives_none(self) -> None:
        transport, ib = _make()
        await transport.connect()
        ib.executions_result = [make_fill("e1.01", report=make_commission_report(""))]
        ((_, fee),) = await transport.get_fills(SINCE)
        assert fee is None

    async def test_report_none_gives_none(self) -> None:
        transport, ib = _make()
        await transport.connect()
        fill = make_fill("e1.01")
        fill.commissionReport = None
        ib.executions_result = [fill]
        ((_, fee),) = await transport.get_fills(SINCE)
        assert fee is None

    async def test_unset_commission_gives_none(self) -> None:
        transport, ib = _make()
        await transport.connect()
        report = make_commission_report("e1.01", commission=UNSET_DOUBLE)
        ib.executions_result = [make_fill("e1.01", report=report)]
        ((_, fee),) = await transport.get_fills(SINCE)
        assert fee is None

    async def test_ordered_by_execution_time_ascending(self) -> None:
        transport, ib = _make()
        await transport.connect()
        t0 = SINCE + timedelta(minutes=1)
        ib.executions_result = [
            _fill_at("c.01", t0 + timedelta(minutes=2)),
            _fill_at("a.01", t0),
            _fill_at("b.01", t0 + timedelta(minutes=1)),
        ]
        result = await transport.get_fills(SINCE)
        assert [f.exec_id for f, _ in result] == ["a.01", "b.01", "c.01"]

    async def test_does_not_publish_to_topics(self) -> None:
        transport, ib = _make()
        fills: list[FillEvent] = []
        fees: list[FeeEvent] = []
        await transport.connect()
        transport.subscribe_fills(fills.append)
        transport.subscribe_fees(fees.append)
        report = make_commission_report("e1.01")
        ib.executions_result = [make_fill("e1.01", report=report)]
        await transport.get_fills(SINCE)
        assert fills == []
        assert fees == []

    async def test_does_not_mark_exec_ids_seen(self) -> None:
        transport, ib = _make()
        fills: list[FillEvent] = []
        fees: list[FeeEvent] = []
        transport.subscribe_fills(fills.append)
        transport.subscribe_fees(fees.append)
        await transport.connect()
        fill = make_fill("e1.01", report=make_commission_report("e1.01"))
        ib.executions_result = [fill]
        await transport.get_fills(SINCE)
        ib.execDetailsEvent(None, fill)
        ib.commissionReportEvent(None, fill, fill.commissionReport)
        assert [f.exec_id for f in fills] == ["e1.01"]
        assert [f.exec_id for f in fees] == ["e1.01"]

    async def test_gateway_delegates(self) -> None:
        transport = MagicMock()
        sentinel: list[Any] = []
        transport.get_fills = AsyncMock(return_value=sentinel)
        gateway = IbGatewayGateway(transport=transport, signer=MagicMock())
        assert await gateway.get_fills(SINCE) is sentinel
        transport.get_fills.assert_awaited_once_with(SINCE)
