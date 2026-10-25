"""Live contract resolution, one contract per asset class (issue #57). READ-ONLY: no orders.

Skipped by default at module level UNLESS IB_GATEWAY_HOST is set (same gate as
test_integration.py). Covers STOCK, CRYPTO, FOREX, FUTURE and the not-found error path.

To run (paper Gateway only; live ports 4001/7496 are refused):
    IB_GATEWAY_HOST=127.0.0.1 IB_GATEWAY_PORT=4002 [IB_CLIENT_ID=957] [IB_PAPER_ACCOUNT=...] \\
        pixi run -e default pytest -m ib_gateway --no-cov \\
        tests/exchanges/interactive_brokers/test_integration_asset_classes_57.py
(Not ``pixi run test``: that task appends tests/ and would also run the order-placing live modules.)

The crypto market-data check is skipped (not failed) when the account lacks a subscription.
"""

from __future__ import annotations

import datetime as dt
import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import pytest

if not os.environ.get("IB_GATEWAY_HOST"):
    pytest.skip(
        "Set IB_GATEWAY_HOST to run integration tests against a live IB Gateway",
        allow_module_level=True,
    )

from market_connector.contracts.instrument import InstrumentRef, InstrumentType
from market_connector.exchanges.interactive_brokers.exceptions import (
    ContractNotFoundError,
    MarketDataPermissionError,
)
from market_connector.exchanges.interactive_brokers.factory import build_ib_gateway
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec

from ._live import live_endpoint

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from market_connector.exchanges.interactive_brokers.ib_gateway import IbGatewayGateway

pytestmark = pytest.mark.ib_gateway

_MIN_DAYS_TO_EXPIRY = 10
_QUARTERLY_MONTHS = (3, 6, 9, 12)


def _paper_spec() -> IbConnectionSpec:
    host, port = live_endpoint()  # fails on live-trading ports before anything connects
    spec = IbConnectionSpec(
        host=host,
        port=port,
        client_id=int(os.environ.get("IB_CLIENT_ID", "957")),
        account_id=os.environ.get("IB_PAPER_ACCOUNT"),
        paper=True,
    )
    assert spec.paper
    return spec


@asynccontextmanager
async def _connected() -> AsyncIterator[IbGatewayGateway]:
    gateway = build_ib_gateway(_paper_spec())
    try:
        await gateway.start()
        yield gateway
    finally:
        await gateway.stop()


def _front_quarterly_month(today: dt.date) -> str:
    """Return YYYYMM of the next Mar/Jun/Sep/Dec at least ~10 days after ``today``.

    Quarterly index futures expire on the third Friday (day <= 21), so the month's
    start is conservatively compared against the cutoff using day 21 as expiry.
    """
    cutoff = today + dt.timedelta(days=_MIN_DAYS_TO_EXPIRY)
    year, month = today.year, today.month
    for _ in range(8):
        if month in _QUARTERLY_MONTHS and dt.date(year, month, 21) >= cutoff:
            return f"{year}{month:02d}"
        month += 1
        if month > 12:
            month, year = 1, year + 1
    raise AssertionError("unreachable: a quarterly month is always within 8 months")


def _ref(symbol: str, kind: InstrumentType, quote: str = "USD", **kw: object) -> InstrumentRef:
    return InstrumentRef(symbol=symbol, instrument_type=kind, quote_currency=quote, **kw)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_stock_aapl_resolves_and_round_trips_by_con_id() -> None:
    ref = _ref("AAPL", InstrumentType.STOCK, primary_exchange="NASDAQ")
    async with _connected() as gateway:
        resolver = gateway.contract_resolver
        resolved = await resolver.resolve(ref)
        assert resolved.native.secType == "STK"
        assert resolved.contract_id == "265598"
        assert resolved.native.primaryExchange == "NASDAQ"

        again = await resolver.resolve(
            InstrumentRef(
                symbol="AAPL",
                instrument_type=InstrumentType.STOCK,
                quote_currency="USD",
                con_id=int(resolved.contract_id),
            )
        )
        assert again.contract_id == resolved.contract_id


@pytest.mark.asyncio
async def test_crypto_btc_usd_paxos_fractional_rules() -> None:
    ref = _ref("BTC", InstrumentType.CRYPTO)
    async with _connected() as gateway:
        resolved = await gateway.contract_resolver.resolve(ref)
        assert resolved.native.secType == "CRYPTO"
        assert resolved.native.exchange == "PAXOS"

        rules = await gateway.get_trading_rules(ref)
        assert rules.min_tick > 0
        assert 0 < rules.size_increment < 1
        assert 0 < rules.min_size < 1


@pytest.mark.asyncio
async def test_forex_eur_usd_idealpro() -> None:
    ref = _ref("EUR", InstrumentType.FOREX)
    async with _connected() as gateway:
        resolved = await gateway.contract_resolver.resolve(ref)
        assert resolved.native.secType == "CASH"
        assert resolved.native.exchange == "IDEALPRO"
        assert resolved.native.symbol == "EUR"
        assert resolved.native.currency == "USD"

        rules = await gateway.get_trading_rules(ref)
        assert rules.min_tick > 0


@pytest.mark.asyncio
async def test_future_mes_front_quarterly_month_on_cme() -> None:
    month = _front_quarterly_month(dt.datetime.now(dt.UTC).date())
    ref = _ref("MES", InstrumentType.FUTURE, exchange_hint="CME", contract_month=month)
    async with _connected() as gateway:
        resolved = await gateway.contract_resolver.resolve(ref)
        native = resolved.native
        assert native.secType == "FUT"
        assert native.exchange == "CME"
        assert native.lastTradeDateOrContractMonth.startswith(month)
        assert native.multiplier


@pytest.mark.asyncio
async def test_unknown_symbol_raises_contract_not_found() -> None:
    ref = _ref("ZZZQXJ", InstrumentType.STOCK)
    async with _connected() as gateway:
        with pytest.raises(ContractNotFoundError) as excinfo:
            await gateway.contract_resolver.resolve(ref)
        assert "ZZZQXJ" in str(excinfo.value)


@pytest.mark.asyncio
async def test_crypto_btc_usd_top_of_book_or_skip_without_permission() -> None:
    ref = _ref("BTC", InstrumentType.CRYPTO)
    async with _connected() as gateway:
        resolved = await gateway.contract_resolver.resolve(ref)
        try:
            bid, ask = await gateway._transport.top_of_book(resolved)
        except MarketDataPermissionError as exc:
            pytest.skip(f"no crypto market-data permission (IB code {exc.error_code})")
        assert 0 < bid <= ask
