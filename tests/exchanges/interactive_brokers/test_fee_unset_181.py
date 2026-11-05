"""Regression for #181: an unset commission on the live path publishes no FeeEvent."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.transport import (
    FeeEvent,
    IbGatewayTransport,
)

from ._doubles import MockIb, make_commission_report, make_fill

pytest.importorskip("ib_async")

_IB_PATH = "market_connector.exchanges.interactive_brokers.transport.IB"
UNSET_DOUBLE = 1.7976931348623157e308


def _make() -> tuple[IbGatewayTransport, MockIb]:
    ib = MockIb()
    ib.managed_accounts = ["DU1111111"]
    with patch(_IB_PATH, return_value=ib):
        transport = IbGatewayTransport(IbConnectionSpec(account_id="DU1111111"))

    async def fake_sleep(_delay: float) -> None:
        await asyncio.sleep(0)

    transport._sleep = fake_sleep
    return transport, ib


async def test_live_unset_commission_publishes_no_fee() -> None:
    transport, ib = _make()
    fees: list[FeeEvent] = []
    transport.subscribe_fees(fees.append)
    await transport.connect()
    report = make_commission_report("e1.01", commission=UNSET_DOUBLE)
    fill = make_fill("e1.01", report=report)
    ib.commissionReportEvent(None, fill, report)
    assert fees == []


async def test_live_unset_then_real_commission_still_publishes() -> None:
    transport, ib = _make()
    fees: list[FeeEvent] = []
    transport.subscribe_fees(fees.append)
    await transport.connect()
    unset = make_commission_report("e1.01", commission=UNSET_DOUBLE)
    fill = make_fill("e1.01", report=unset)
    ib.commissionReportEvent(None, fill, unset)
    real = make_commission_report("e1.01", commission=1.05)
    ib.commissionReportEvent(None, fill, real)
    assert [f.exec_id for f in fees] == ["e1.01"]
