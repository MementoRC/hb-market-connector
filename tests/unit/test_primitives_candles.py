# tests/unit/test_primitives_candles.py
"""Tests for CandleData primitive (Stage 3)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from market_connector.primitives import CandleData


class TestCandleDataFields:
    def test_all_fields_accessible(self):
        c = CandleData(
            trading_pair="AAPL-USD",
            timestamp=1_700_000_000.0,
            interval="1m",
            open=Decimal("150.00"),
            high=Decimal("151.00"),
            low=Decimal("149.50"),
            close=Decimal("150.75"),
            volume=Decimal("1234"),
        )
        assert c.trading_pair == "AAPL-USD"
        assert c.timestamp == 1_700_000_000.0
        assert c.interval == "1m"
        assert c.open == Decimal("150.00")
        assert c.high == Decimal("151.00")
        assert c.low == Decimal("149.50")
        assert c.close == Decimal("150.75")
        assert c.volume == Decimal("1234")

    def test_is_frozen(self):
        c = CandleData(
            trading_pair="AAPL-USD",
            timestamp=1_700_000_000.0,
            interval="1m",
            open=Decimal("150"),
            high=Decimal("151"),
            low=Decimal("149"),
            close=Decimal("150"),
            volume=Decimal("100"),
        )
        with pytest.raises(FrozenInstanceError):
            c.close = Decimal("999")  # type: ignore[misc]

    def test_equality(self):
        kwargs = dict(
            trading_pair="AAPL-USD",
            timestamp=1_700_000_000.0,
            interval="1m",
            open=Decimal("1"),
            high=Decimal("2"),
            low=Decimal("0"),
            close=Decimal("1"),
            volume=Decimal("10"),
        )
        assert CandleData(**kwargs) == CandleData(**kwargs)

    def test_has_slots(self):
        assert "__slots__" in CandleData.__dict__

    def test_volume_zero_is_valid(self):
        c = CandleData(
            trading_pair="AAPL-USD",
            timestamp=1_700_000_000.0,
            interval="1m",
            open=Decimal("150"),
            high=Decimal("150"),
            low=Decimal("150"),
            close=Decimal("150"),
            volume=Decimal("0"),
        )
        assert c.volume == Decimal("0")
