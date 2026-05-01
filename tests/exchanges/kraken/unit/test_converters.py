"""Unit tests for market_connector.exchanges.kraken.converters."""

from __future__ import annotations

import pytest

from market_connector.exchanges.kraken.converters import kraken_ordertype_from_hb
from market_connector.primitives import OrderType


class TestKrakenOrdertypeFromHbStrings:
    """String-input path: case-insensitive passthrough and normalisation."""

    def test_limit_uppercase(self) -> None:
        assert kraken_ordertype_from_hb("LIMIT") == "limit"

    def test_market_lowercase(self) -> None:
        assert kraken_ordertype_from_hb("market") == "market"

    def test_limit_mixed_case(self) -> None:
        assert kraken_ordertype_from_hb("Limit") == "limit"

    def test_stop_loss_hyphenated_string(self) -> None:
        # Kraken-native hyphenated string normalised to map key
        assert kraken_ordertype_from_hb("STOP-LOSS") == "stop-loss"

    def test_stop_loss_underscore_string(self) -> None:
        # Underscore variant (as would appear from repr of enum name)
        assert kraken_ordertype_from_hb("STOP_LOSS") == "stop-loss"

    def test_take_profit_string(self) -> None:
        assert kraken_ordertype_from_hb("take_profit") == "take-profit"

    def test_trailing_stop_string(self) -> None:
        assert kraken_ordertype_from_hb("trailing_stop") == "trailing-stop"

    def test_limit_maker_string(self) -> None:
        # LIMIT_MAKER maps to Kraken "limit"
        assert kraken_ordertype_from_hb("LIMIT_MAKER") == "limit"

    def test_unknown_string_raises(self) -> None:
        with pytest.raises(ValueError, match="Cannot map order type"):
            kraken_ordertype_from_hb("iceberg_order")


class TestKrakenOrdertypeFromHbEnum:
    """Enum-input path: market_connector.primitives.OrderType."""

    def test_limit_enum(self) -> None:
        assert kraken_ordertype_from_hb(OrderType.LIMIT) == "limit"

    def test_market_enum(self) -> None:
        assert kraken_ordertype_from_hb(OrderType.MARKET) == "market"

    def test_limit_maker_enum(self) -> None:
        assert kraken_ordertype_from_hb(OrderType.LIMIT_MAKER) == "limit"


class TestKrakenOrdertypeFromHbDuckTyped:
    """Duck-typed path: any object exposing a .name attribute."""

    class _FakeOrderType:
        def __init__(self, name: str) -> None:
            self.name = name

    def test_duck_typed_limit(self) -> None:
        assert kraken_ordertype_from_hb(self._FakeOrderType("LIMIT")) == "limit"

    def test_duck_typed_stop_loss(self) -> None:
        assert kraken_ordertype_from_hb(self._FakeOrderType("STOP_LOSS")) == "stop-loss"

    def test_duck_typed_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="Cannot map order type"):
            kraken_ordertype_from_hb(self._FakeOrderType("ICEBERG"))


class TestKrakenOrdertypeFromHbTriggerKind:
    """trigger_kind is reserved/ignored but must not break the call."""

    def test_trigger_kind_ignored(self) -> None:
        assert kraken_ordertype_from_hb(OrderType.LIMIT, trigger_kind="index") == "limit"

    def test_trigger_kind_none(self) -> None:
        assert kraken_ordertype_from_hb("MARKET", trigger_kind=None) == "market"
