"""Tests for IB fills, fees, de-duplication and reconcile-on-(re)connect (issue #52).

All run against MockIb; fills and reports are SimpleNamespace fakes.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from dataclasses import dataclass
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from market_connector.exchanges.interactive_brokers import transport as transport_module
from market_connector.exchanges.interactive_brokers.ib_gateway import IbGatewayGateway
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.transport import (
    FEE_TOPIC,
    FILL_TOPIC,
    FeeEvent,
    FillEvent,
    IbGatewayTransport,
)
from market_connector.orders import TradeType

from ._doubles import MockIb, make_commission_report, make_fill

_IB_PATH = "market_connector.exchanges.interactive_brokers.transport.IB"


def _make(
    spec: IbConnectionSpec | None = None,
) -> tuple[IbGatewayTransport, MockIb, list[FillEvent], list[FeeEvent]]:
    """Build a transport over MockIb that records fills and fees."""
    ib = MockIb()
    with patch(_IB_PATH, return_value=ib):
        transport = IbGatewayTransport(spec or IbConnectionSpec())

    async def fake_sleep(_delay: float) -> None:
        await asyncio.sleep(0)

    transport._sleep = fake_sleep
    fills: list[FillEvent] = []
    fees: list[FeeEvent] = []
    transport.subscribe_fills(fills.append)
    transport.subscribe_fees(fees.append)
    return transport, ib, fills, fees


class TestLiveFills:
    async def test_partial_fill_gives_two_events_with_growing_cum_qty(self) -> None:
        transport, ib, fills, _ = _make()
        await transport.connect()
        ib.execDetailsEvent(None, make_fill("e1.01", shares=4, cum_qty=4))
        ib.execDetailsEvent(None, make_fill("e2.01", shares=6, cum_qty=10))
        assert [f.cum_qty for f in fills] == [Decimal(4), Decimal(10)]
        assert [f.size for f in fills] == [Decimal(4), Decimal(6)]
        assert not any(f.is_correction or f.reconciled for f in fills)

    async def test_fill_fields_and_decimal_conversion(self) -> None:
        transport, ib, fills, _ = _make()
        await transport.connect()
        ib.execDetailsEvent(None, make_fill("e1.01", price=0.1, shares=0.3, cum_qty=0.3))
        (fill,) = fills
        assert fill.price == Decimal("0.1")
        assert fill.size == Decimal("0.3")
        assert isinstance(fill.price, Decimal)
        assert (fill.exec_id, fill.order_id, fill.perm_id) == ("e1.01", 11, 987654)
        assert (fill.con_id, fill.symbol, fill.account) == (265598, "AAPL", "DU123")
        assert fill.time is not None

    @pytest.mark.parametrize(
        ("ib_side", "expected"), [("BOT", TradeType.BUY), ("SLD", TradeType.SELL)]
    )
    async def test_side_mapping(self, ib_side: str, expected: TradeType) -> None:
        transport, ib, fills, _ = _make()
        await transport.connect()
        ib.execDetailsEvent(None, make_fill("e1.01", side=ib_side))
        assert fills[0].side is expected

    async def test_duplicate_exec_id_dropped(self) -> None:
        transport, ib, fills, _ = _make()
        await transport.connect()
        ib.execDetailsEvent(None, make_fill("e1.01"))
        ib.execDetailsEvent(None, make_fill("e1.01"))
        assert len(fills) == 1

    async def test_correction_suffix_flagged(self) -> None:
        transport, ib, fills, _ = _make()
        await transport.connect()
        ib.execDetailsEvent(None, make_fill("e1.01"))
        ib.execDetailsEvent(None, make_fill("e1.02"))
        ib.execDetailsEvent(None, make_fill("e1.02"))
        assert [(f.exec_id, f.is_correction) for f in fills] == [("e1.01", False), ("e1.02", True)]

    async def test_exec_id_without_dot_is_its_own_base(self) -> None:
        transport, ib, fills, _ = _make()
        await transport.connect()
        ib.execDetailsEvent(None, make_fill("plain"))
        ib.execDetailsEvent(None, make_fill("plain"))
        assert len(fills) == 1

    async def test_stale_version_after_correction_not_published(self) -> None:
        """Correction .02 arrives first; older .01 later is not published."""
        transport, ib, fills, _ = _make()
        await transport.connect()
        ib.execDetailsEvent(None, make_fill("e1.02"))
        ib.execDetailsEvent(None, make_fill("e1.01"))
        assert [(f.exec_id, f.is_correction) for f in fills] == [("e1.02", False)]

    async def test_multiple_corrections_idempotent_to_stale(self) -> None:
        """Sequence .01, .02, .01 again: exactly two fills published."""
        transport, ib, fills, _ = _make()
        await transport.connect()
        ib.execDetailsEvent(None, make_fill("e1.01"))
        ib.execDetailsEvent(None, make_fill("e1.02"))
        ib.execDetailsEvent(None, make_fill("e1.01"))
        assert [(f.exec_id, f.is_correction) for f in fills] == [
            ("e1.01", False),
            ("e1.02", True),
        ]


class TestLiveFees:
    async def test_late_commission_follows_fill_and_duplicate_dropped(self) -> None:
        transport, ib, fills, fees = _make()
        order: list[str] = []
        transport.subscribe_fills(lambda _e: order.append("fill"))
        transport.subscribe_fees(lambda _e: order.append("fee"))
        await transport.connect()
        fill = make_fill("e1.01")
        ib.execDetailsEvent(None, fill)
        assert fees == []  # fill is not held back waiting for its fee
        report = make_commission_report("e1.01", commission=1.05, currency="USD")
        ib.commissionReportEvent(None, fill, report)
        ib.commissionReportEvent(None, fill, report)
        assert order == ["fill", "fee"]
        assert len(fills) == 1
        assert fees == [FeeEvent("e1.01", 11, Decimal("1.05"), "USD")]

    async def test_empty_commission_exec_id_ignored(self) -> None:
        transport, ib, _, fees = _make()
        await transport.connect()
        ib.commissionReportEvent(None, make_fill("e1.01"), make_commission_report(""))
        assert fees == []


@dataclass
class _StubExecutionFilter:
    clientId: int = 0  # noqa: N815
    acctCode: str = ""  # noqa: N815


@pytest.fixture(autouse=True)
def _ib_async_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``ib_async.ExecutionFilter`` when ib_async is not installed (e.g. CI py312)."""
    if importlib.util.find_spec("ib_async") is None:
        stub = types.ModuleType("ib_async")
        stub.ExecutionFilter = _StubExecutionFilter  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "ib_async", stub)


class TestReconcile:
    async def test_first_connect_passes_filter_and_emits_reconciled(self) -> None:
        transport, ib, fills, fees = _make(IbConnectionSpec(client_id=7, account_id="DU123"))
        ib.executions_result = [
            make_fill("e1.01", report=make_commission_report("e1.01")),
            make_fill("e2.01"),  # report not yet populated: fill only
        ]
        await transport.connect()
        (exec_filter,) = ib.req_executions_filters
        assert (exec_filter.clientId, exec_filter.acctCode) == (7, "DU123")
        assert [(f.exec_id, f.reconciled) for f in fills] == [("e1.01", True), ("e2.01", True)]
        assert [(f.exec_id, f.reconciled) for f in fees] == [("e1.01", True)]

    async def test_blank_account_filter(self) -> None:
        transport, ib, _, _ = _make()
        await transport.connect()
        assert ib.req_executions_filters[0].acctCode == ""

    async def test_reconcile_runs_after_market_data_type(self) -> None:
        transport, ib, _, _ = _make()
        seen: list[int] = []
        original = ib.reqExecutionsAsync

        async def spy(exec_filter: object = None) -> list[object]:
            seen.append(len(ib.market_data_type_calls))
            return await original(exec_filter)

        ib.reqExecutionsAsync = spy  # type: ignore[method-assign]
        await transport.connect()
        assert seen == [1]

    async def test_fill_seen_live_not_reemitted(self) -> None:
        transport, ib, fills, fees = _make()
        await transport.connect()
        live = make_fill("e1.01", report=make_commission_report("e1.01"))
        ib.execDetailsEvent(None, live)
        ib.commissionReportEvent(None, live, live.commissionReport)
        ib.executions_result = [live, make_fill("e2.01")]
        ib.simulate_drop()
        assert transport._supervisor is not None
        await transport._supervisor
        assert [(f.exec_id, f.reconciled) for f in fills] == [("e1.01", False), ("e2.01", True)]
        assert len(fees) == 1

    async def test_reconcile_runs_again_after_reconnect(self) -> None:
        transport, ib, fills, _ = _make()
        await transport.connect()
        assert len(ib.req_executions_filters) == 1
        ib.executions_result = [make_fill("missed.01")]
        ib.simulate_drop()
        assert transport._supervisor is not None
        await transport._supervisor
        assert len(ib.req_executions_filters) == 2
        assert [(f.exec_id, f.reconciled) for f in fills] == [("missed.01", True)]
        assert len(ib.execDetailsEvent._handlers) == 1  # handlers not doubled

    async def test_reconcile_failure_does_not_fail_connect(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        transport, ib, _, _ = _make()
        ib.executions_error = RuntimeError("boom")
        with caplog.at_level("WARNING"):
            await transport.connect()
        assert transport._error_router.is_connected
        assert "reconcile failed" in caplog.text


class TestHandlersAndTopics:
    async def test_handlers_detached_on_disconnect(self) -> None:
        transport, ib, fills, _ = _make()
        await transport.connect()
        assert len(ib.execDetailsEvent._handlers) == 1
        assert len(ib.commissionReportEvent._handlers) == 1
        await transport.disconnect()
        assert ib.execDetailsEvent._handlers == []
        assert ib.commissionReportEvent._handlers == []
        ib.execDetailsEvent(None, make_fill("e1.01"))
        assert fills == []

    async def test_dedupe_cap_evicts_oldest(self) -> None:
        transport, ib, fills, fees = _make()
        await transport.connect()
        with patch.object(transport_module, "_MAX_TRACKED_EXECUTIONS", 2):
            for i in (1, 2, 3):
                fill = make_fill(f"e{i}.01")
                ib.execDetailsEvent(None, fill)
                ib.commissionReportEvent(None, fill, make_commission_report(f"e{i}.01"))
            assert len(transport._seen_fills) == 2
            assert len(transport._seen_fees) == 2
            ib.execDetailsEvent(None, make_fill("e1.01"))  # evicted: treated as new
            ib.commissionReportEvent(None, make_fill("e1.01"), make_commission_report("e1.01"))
        assert len(fills) == 4
        assert len(fees) == 4

    async def test_handle_close_unsubscribes(self) -> None:
        transport, ib, fills, fees = _make()
        fill_handle = transport.subscribe_fills(lambda _e: None)
        fee_handle = transport.subscribe_fees(lambda _e: None)
        assert (fill_handle.channel, fee_handle.channel) == (FILL_TOPIC, FEE_TOPIC)
        fill_handle.close()
        fee_handle.close()
        await transport.connect()
        ib.execDetailsEvent(None, make_fill("e1.01"))
        assert len(fills) == 1  # the original recorder still receives

    def test_events_are_frozen_with_defaults(self) -> None:
        fee = FeeEvent("e1", 1, Decimal(1), "USD")
        assert fee.reconciled is False
        with pytest.raises(AttributeError):
            fee.fee = Decimal(2)  # type: ignore[misc]

    async def test_gateway_passthroughs(self) -> None:
        transport, ib, _, _ = _make()
        gateway = IbGatewayGateway(transport=transport, signer=MagicMock())
        fills: list[FillEvent] = []
        fees: list[FeeEvent] = []
        gateway.subscribe_fills(fills.append)
        gateway.subscribe_fees(fees.append)
        await gateway.start()
        fill = make_fill("e1.01")
        ib.execDetailsEvent(None, fill)
        ib.commissionReportEvent(None, fill, make_commission_report("e1.01"))
        assert len(fills) == 1
        assert len(fees) == 1
