"""Red acceptance tests for issue #57b — default IB exchange per instrument type.

Decision: pair strings stay STOCK-only; non-stock instruments come in as InstrumentRef.
"""

from __future__ import annotations

# ruff: noqa: N815  (FakeContract mirrors ib_async camelCase field names)
import sys
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from market_connector.contracts.instrument import InstrumentRef, InstrumentType
from market_connector.exchanges.interactive_brokers.contract_resolver import IbContractResolver
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.transport import IbGatewayTransport


@dataclass
class FakeContract:
    """Stand-in for ib_async.Contract (same harness as test_contract_fields_57a)."""

    secType: str = ""
    conId: int = 0
    symbol: str = ""
    lastTradeDateOrContractMonth: str = ""
    strike: float = 0.0
    right: str = ""
    multiplier: str = ""
    exchange: str = ""
    primaryExchange: str = ""
    currency: str = ""
    localSymbol: str = ""
    tradingClass: str = ""


class FakeIb:
    """Records the template passed to reqContractDetailsAsync."""

    def __init__(self) -> None:
        self.templates: list[FakeContract] = []

    async def reqContractDetailsAsync(self, template: FakeContract) -> list:  # noqa: N802
        self.templates.append(template)
        return []


@pytest.fixture
def fake_ib() -> FakeIb:
    return FakeIb()


@pytest.fixture
def transport(fake_ib: FakeIb):
    fake_module = MagicMock()
    fake_module.Contract = FakeContract
    spec = IbConnectionSpec(host="127.0.0.1", port=4002, client_id=1, paper=True)
    with (
        patch.dict(sys.modules, {"ib_async": fake_module}),
        patch(
            "market_connector.exchanges.interactive_brokers.transport.IB",
            return_value=fake_ib,
        ),
    ):
        yield IbGatewayTransport(spec)


async def _template(transport: Any, fake_ib: FakeIb, ref: InstrumentRef) -> FakeContract:
    await transport._resolve_via_ib(ref)
    return fake_ib.templates[-1]


def _ref(symbol: str, kind: InstrumentType, quote: str | None = "USD", **kw: Any) -> InstrumentRef:
    return InstrumentRef(symbol=symbol, instrument_type=kind, quote_currency=quote, **kw)


class TestDefaultExchangeWithoutHint:
    @pytest.mark.asyncio
    async def test_crypto_defaults_to_paxos(self, transport, fake_ib) -> None:
        t = await _template(transport, fake_ib, _ref("ETH", InstrumentType.CRYPTO))
        assert (t.secType, t.exchange, t.currency) == ("CRYPTO", "PAXOS", "USD")

    @pytest.mark.asyncio
    async def test_forex_defaults_to_idealpro(self, transport, fake_ib) -> None:
        t = await _template(transport, fake_ib, _ref("EUR", InstrumentType.FOREX))
        assert (t.secType, t.exchange, t.symbol, t.currency) == ("CASH", "IDEALPRO", "EUR", "USD")

    @pytest.mark.asyncio
    async def test_stock_defaults_to_smart(self, transport, fake_ib) -> None:
        t = await _template(transport, fake_ib, _ref("AAPL", InstrumentType.STOCK))
        assert t.exchange == "SMART"

    @pytest.mark.asyncio
    async def test_option_defaults_to_smart(self, transport, fake_ib) -> None:
        t = await _template(transport, fake_ib, _ref("AAPL", InstrumentType.OPTION))
        assert t.exchange == "SMART"


class TestExplicitHintWins:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("kind", "symbol", "hint"),
        [
            (InstrumentType.CRYPTO, "ETH", "ZEROHASH"),
            (InstrumentType.FOREX, "EUR", "FXCONV"),
            (InstrumentType.STOCK, "AAPL", "ARCA"),
        ],
    )
    async def test_hint_overrides_default(self, transport, fake_ib, kind, symbol, hint) -> None:
        t = await _template(transport, fake_ib, _ref(symbol, kind, exchange_hint=hint))
        assert t.exchange == hint


class TestFutureRequiresExchange:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "extra",
        [{}, {"local_symbol": "ESZ6"}, {"contract_month": "202612"}],
    )
    async def test_future_without_hint_raises(self, transport, fake_ib, extra) -> None:
        ref = _ref("ES", InstrumentType.FUTURE, **extra)
        with pytest.raises(
            ValueError, match=r"(?is)ES.*exchange.*required.*future|ES.*future.*exchange.*required"
        ):
            await transport._resolve_via_ib(ref)
        assert fake_ib.templates == []

    @pytest.mark.asyncio
    async def test_future_with_hint_resolves(self, transport, fake_ib) -> None:
        ref = _ref("ES", InstrumentType.FUTURE, exchange_hint="CME", contract_month="202612")
        t = await _template(transport, fake_ib, ref)
        assert (t.secType, t.exchange, t.lastTradeDateOrContractMonth) == ("FUT", "CME", "202612")


class TestConIdPathUnchanged:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", list(InstrumentType))
    async def test_con_id_without_hint_keeps_smart(self, transport, fake_ib, kind) -> None:
        t = await _template(transport, fake_ib, _ref("ES", kind, con_id=12345))
        assert (t.conId, t.exchange) == (12345, "SMART")


class TestPairStringsStayStock:
    @pytest.mark.parametrize("pair", ["AAPL-USD", "ETH-USD"])
    def test_pair_parses_as_stock(self, pair: str) -> None:
        class _NoTransport:
            pass

        resolver = IbContractResolver(_NoTransport())  # type: ignore[arg-type]
        ref = resolver._parse_pair(pair)
        assert ref.instrument_type is InstrumentType.STOCK
