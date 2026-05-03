"""Tests for InstrumentRef primitive and supporting enums."""

from datetime import date
from decimal import Decimal

import pytest

from market_connector.contracts.instrument import (
    InstrumentRef,
    InstrumentType,
    OptionRight,
)


class TestInstrumentType:
    def test_values_are_canonical_strings(self):
        assert InstrumentType.STOCK == "STOCK"
        assert InstrumentType.OPTION == "OPTION"
        assert InstrumentType.FUTURE == "FUTURE"
        assert InstrumentType.FOREX == "FOREX"
        assert InstrumentType.CRYPTO == "CRYPTO"
        assert InstrumentType.BOND == "BOND"
        assert InstrumentType.CFD == "CFD"
        assert InstrumentType.INDEX == "INDEX"

    def test_is_str_enum(self):
        assert isinstance(InstrumentType.STOCK, str)


class TestOptionRight:
    def test_values(self):
        assert OptionRight.CALL == "CALL"
        assert OptionRight.PUT == "PUT"


class TestInstrumentRef:
    def test_minimal_stock_construction(self):
        ref = InstrumentRef(symbol="AAPL", instrument_type=InstrumentType.STOCK)
        assert ref.symbol == "AAPL"
        assert ref.instrument_type == InstrumentType.STOCK
        assert ref.quote_currency is None
        assert ref.exchange_hint is None
        assert ref.expiry is None
        assert ref.strike is None
        assert ref.option_right is None
        assert ref.extras == {}

    def test_full_option_construction(self):
        ref = InstrumentRef(
            symbol="AAPL",
            instrument_type=InstrumentType.OPTION,
            quote_currency="USD",
            exchange_hint="SMART",
            expiry=date(2026, 6, 19),
            strike=Decimal("180.00"),
            option_right=OptionRight.CALL,
            extras={"trading_class": "AAPL"},
        )
        assert ref.option_right == OptionRight.CALL
        assert ref.strike == Decimal("180.00")
        assert ref.extras["trading_class"] == "AAPL"

    def test_frozen(self):
        ref = InstrumentRef(symbol="AAPL", instrument_type=InstrumentType.STOCK)
        with pytest.raises((AttributeError, TypeError)):
            ref.symbol = "TSLA"  # type: ignore[misc]

    def test_equality(self):
        a = InstrumentRef(symbol="BTC", instrument_type=InstrumentType.CRYPTO, quote_currency="USD")
        b = InstrumentRef(symbol="BTC", instrument_type=InstrumentType.CRYPTO, quote_currency="USD")
        assert a == b

    def test_hashable(self):
        ref = InstrumentRef(
            symbol="EUR", instrument_type=InstrumentType.FOREX, quote_currency="USD"
        )
        d = {ref: "value"}
        assert d[ref] == "value"

    def test_extras_default_is_independent(self):
        a = InstrumentRef(symbol="A", instrument_type=InstrumentType.STOCK)
        b = InstrumentRef(symbol="B", instrument_type=InstrumentType.STOCK)
        assert a.extras is not b.extras  # mutability isolation
