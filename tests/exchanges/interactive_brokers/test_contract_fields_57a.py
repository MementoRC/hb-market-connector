"""Red acceptance tests for issue #57a — richer IB contract fields and conId cache.

Covers: InstrumentRef new fields, the Contract template built by
``IbGatewayTransport._resolve_via_ib`` and the resolver's conId cache index.

Decision (criterion 2c): when ``con_id`` is set and no ``exchange_hint`` is given,
the template ``exchange`` is "SMART" (consistent with the symbol-based default).
"""

# ruff: noqa: N815  (FakeContract mirrors ib_async camelCase field names)
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from market_connector.contracts.instrument import InstrumentRef, InstrumentType, OptionRight
from market_connector.exchanges.interactive_brokers.contract_resolver import IbContractResolver
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.transport import IbGatewayTransport


@dataclass
class FakeContract:
    """Hand-written stand-in for ib_async.Contract with the same field defaults."""

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


async def _template(transport: IbGatewayTransport, fake_ib: FakeIb, ref: Any) -> FakeContract:
    await transport._resolve_via_ib(ref)
    return fake_ib.templates[-1]


def _ref(**kwargs: Any) -> InstrumentRef:
    kwargs.setdefault("symbol", "AAPL")
    kwargs.setdefault("instrument_type", InstrumentType.STOCK)
    return InstrumentRef(**kwargs)


class TestInstrumentRefFields:
    def test_new_fields_default_to_none(self) -> None:
        ref = _ref(quote_currency="USD")
        assert ref.con_id is None
        assert ref.primary_exchange is None
        assert ref.local_symbol is None
        assert ref.contract_month is None

    def test_new_fields_accepted(self) -> None:
        ref = _ref(
            con_id=265598, primary_exchange="NASDAQ", local_symbol="AAPL", contract_month="202612"
        )
        assert (ref.con_id, ref.primary_exchange) == (265598, "NASDAQ")
        assert (ref.local_symbol, ref.contract_month) == ("AAPL", "202612")

    def test_refs_differing_only_in_con_id_are_unequal(self) -> None:
        a, b = _ref(con_id=1), _ref(con_id=2)
        assert a != b
        assert len({a, b}) == 2

    @pytest.mark.parametrize("field", ["primary_exchange", "local_symbol", "contract_month"])
    def test_other_new_fields_participate_in_equality(self, field: str) -> None:
        assert _ref(**{field: "X"}) != _ref(**{field: "Y"})

    def test_refs_without_new_fields_keep_equality_and_hash(self) -> None:
        a = _ref(quote_currency="USD")
        b = _ref(quote_currency="USD")
        assert a == b
        assert hash(a) == hash(b)


class TestContractTemplate:
    @pytest.mark.asyncio
    async def test_plain_stock_template_unchanged(self, transport, fake_ib) -> None:
        t = await _template(transport, fake_ib, _ref(quote_currency="USD"))
        assert (t.symbol, t.secType, t.currency, t.exchange) == ("AAPL", "STK", "USD", "SMART")
        assert t.primaryExchange == ""
        assert t.conId == 0

    @pytest.mark.asyncio
    async def test_primary_exchange(self, transport, fake_ib) -> None:
        t = await _template(
            transport, fake_ib, _ref(quote_currency="USD", primary_exchange="NASDAQ")
        )
        assert t.primaryExchange == "NASDAQ"

    @pytest.mark.asyncio
    async def test_con_id_resolves_by_con_id_only(self, transport, fake_ib) -> None:
        t = await _template(transport, fake_ib, _ref(quote_currency="USD", con_id=265598))
        assert t.conId == 265598
        assert (t.symbol, t.secType, t.currency) == ("", "", "")
        assert t.exchange == "SMART"  # documented default when no exchange_hint

    @pytest.mark.asyncio
    async def test_con_id_with_exchange_hint(self, transport, fake_ib) -> None:
        ref = _ref(quote_currency="USD", con_id=265598, exchange_hint="NASDAQ")
        t = await _template(transport, fake_ib, ref)
        assert t.conId == 265598
        assert t.exchange == "NASDAQ"

    @pytest.mark.asyncio
    async def test_future_contract_month(self, transport, fake_ib) -> None:
        ref = _ref(
            symbol="ES",
            instrument_type=InstrumentType.FUTURE,
            exchange_hint="CME",
            contract_month="202612",
        )
        t = await _template(transport, fake_ib, ref)
        assert t.secType == "FUT"
        assert t.lastTradeDateOrContractMonth == "202612"
        assert t.exchange == "CME"

    @pytest.mark.asyncio
    async def test_expiry_used_when_no_contract_month(self, transport, fake_ib) -> None:
        ref = _ref(symbol="ES", instrument_type=InstrumentType.FUTURE, expiry=date(2026, 12, 18))
        t = await _template(transport, fake_ib, ref)
        assert t.lastTradeDateOrContractMonth == "20261218"

    @pytest.mark.asyncio
    async def test_contract_month_wins_over_expiry(self, transport, fake_ib) -> None:
        ref = _ref(
            symbol="ES",
            instrument_type=InstrumentType.FUTURE,
            expiry=date(2026, 12, 18),
            contract_month="202612",
        )
        t = await _template(transport, fake_ib, ref)
        assert t.lastTradeDateOrContractMonth == "202612"

    @pytest.mark.asyncio
    async def test_local_symbol(self, transport, fake_ib) -> None:
        ref = _ref(symbol="ES", instrument_type=InstrumentType.FUTURE, local_symbol="ESZ6")
        t = await _template(transport, fake_ib, ref)
        assert t.localSymbol == "ESZ6"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("right", "ib_right"), [(OptionRight.CALL, "C"), (OptionRight.PUT, "P")]
    )
    async def test_option_strike_and_right(self, transport, fake_ib, right, ib_right) -> None:
        ref = _ref(
            instrument_type=InstrumentType.OPTION,
            strike=Decimal("150"),
            option_right=right,
            expiry=date(2026, 12, 18),
        )
        t = await _template(transport, fake_ib, ref)
        assert t.secType == "OPT"
        assert t.strike == 150.0
        assert t.right == ib_right


def _details(con_id: int) -> SimpleNamespace:
    contract = SimpleNamespace(conId=con_id, primaryExchange="NASDAQ")
    return SimpleNamespace(contract=contract, tradingHours="")


class CountingTransport:
    def __init__(self, con_id: int) -> None:
        self.calls = 0
        self._con_id = con_id

    async def _resolve_via_ib(self, ref: InstrumentRef) -> list:
        self.calls += 1
        return [_details(self._con_id)]


class TestConIdCache:
    @pytest.mark.asyncio
    async def test_con_id_ref_served_from_symbol_resolution(self) -> None:
        transport = CountingTransport(265598)
        resolver = IbContractResolver(transport)  # type: ignore[arg-type]
        first = await resolver.resolve(_ref(quote_currency="USD"))
        second = await resolver.resolve(_ref(quote_currency="USD", con_id=265598))
        assert transport.calls == 1
        assert second is first

    @pytest.mark.asyncio
    async def test_same_ref_twice_calls_transport_once(self) -> None:
        transport = CountingTransport(265598)
        resolver = IbContractResolver(transport)  # type: ignore[arg-type]
        ref = _ref(quote_currency="USD")
        await resolver.resolve(ref)
        await resolver.resolve(ref)
        assert transport.calls == 1

    @pytest.mark.asyncio
    async def test_invalidate_clears_con_id_index(self) -> None:
        transport = CountingTransport(265598)
        resolver = IbContractResolver(transport)  # type: ignore[arg-type]
        a = _ref(quote_currency="USD")
        await resolver.resolve(a)
        resolver.invalidate(a)
        await resolver.resolve(_ref(quote_currency="USD", con_id=265598))
        assert transport.calls == 2
