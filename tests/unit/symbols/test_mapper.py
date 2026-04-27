"""Tests for symbol mapper implementations (TDD — Task 9)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from market_connector.exceptions import UnknownPairError
from market_connector.symbols import (
    IdentityMapper,
    RuleBasedMapper,
    SymbolMapper,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_KRAKEN_TO_HB: dict[str, str] = {"XBT": "BTC", "ZUSD": "USD"}
_KRAKEN_FROM_HB: dict[str, str] = {"BTC": "XBT", "USD": "ZUSD"}


# ---------------------------------------------------------------------------
# 1. TestIdentityMapper
# ---------------------------------------------------------------------------


class TestIdentityMapper:
    def test_to_exchange_pair_default_separator(self) -> None:
        mapper = IdentityMapper()
        assert mapper.to_exchange_pair("BTC-USD") == "BTC-USD"

    def test_round_trip(self) -> None:
        mapper = IdentityMapper()
        pair = "ETH-USDC"
        assert mapper.from_exchange_pair(mapper.to_exchange_pair(pair)) == pair

    def test_to_exchange_asset_identity(self) -> None:
        mapper = IdentityMapper()
        assert mapper.to_exchange_asset("BTC") == "BTC"

    def test_from_exchange_asset_identity(self) -> None:
        mapper = IdentityMapper()
        assert mapper.from_exchange_asset("ETH") == "ETH"

    def test_custom_separator_to(self) -> None:
        mapper = IdentityMapper(separator="_")
        assert mapper.to_exchange_pair("BTC-USD") == "BTC_USD"

    def test_custom_separator_from(self) -> None:
        mapper = IdentityMapper(separator="_")
        assert mapper.from_exchange_pair("BTC_USD") == "BTC-USD"

    def test_implements_protocol(self) -> None:
        """IdentityMapper satisfies the SymbolMapper structural protocol."""
        mapper: SymbolMapper = IdentityMapper()
        assert mapper.to_exchange_pair("BTC-USD") == "BTC-USD"


# ---------------------------------------------------------------------------
# 2. TestRuleBasedMapperNoSeparator
# ---------------------------------------------------------------------------


class TestRuleBasedMapperNoSeparator:
    def setup_method(self) -> None:
        self.mapper = RuleBasedMapper(
            separator=None,
            known_quote_assets=("USDT", "USD"),
        )

    def test_from_exchange_pair_basic(self) -> None:
        assert self.mapper.from_exchange_pair("BTCUSDT") == "BTC-USDT"

    def test_longest_quote_wins(self) -> None:
        """BTCUSDT with quotes (USD, USDT) → USDT wins (longer suffix)."""
        mapper = RuleBasedMapper(
            separator=None,
            known_quote_assets=("USD", "USDT"),
        )
        assert mapper.from_exchange_pair("BTCUSDT") == "BTC-USDT"

    def test_to_exchange_pair_basic(self) -> None:
        assert self.mapper.to_exchange_pair("BTC-USDT") == "BTCUSDT"

    def test_unknown_pair_raises(self) -> None:
        with pytest.raises(UnknownPairError):
            self.mapper.from_exchange_pair("BTCBADQUOTE")

    def test_round_trip_no_sep(self) -> None:
        pair = "ETH-USDT"
        assert self.mapper.from_exchange_pair(self.mapper.to_exchange_pair(pair)) == pair


# ---------------------------------------------------------------------------
# 3. TestRuleBasedMapperKrakenAliases
# ---------------------------------------------------------------------------


class TestRuleBasedMapperKrakenAliases:
    def setup_method(self) -> None:
        self.mapper = RuleBasedMapper(
            separator=None,
            known_quote_assets=("ZUSD",),
            asset_aliases_to_hb=_KRAKEN_TO_HB,
            asset_aliases_from_hb=_KRAKEN_FROM_HB,
        )

    def test_from_exchange_pair(self) -> None:
        assert self.mapper.from_exchange_pair("XBTZUSD") == "BTC-USD"

    def test_to_exchange_pair(self) -> None:
        assert self.mapper.to_exchange_pair("BTC-USD") == "XBTZUSD"

    def test_round_trip_aliases(self) -> None:
        pair = "BTC-USD"
        assert self.mapper.from_exchange_pair(self.mapper.to_exchange_pair(pair)) == pair


# ---------------------------------------------------------------------------
# 4. TestRuleBasedMapperWithSeparator (Kraken WS-style)
# ---------------------------------------------------------------------------


class TestRuleBasedMapperWithSeparator:
    def setup_method(self) -> None:
        self.mapper = RuleBasedMapper(
            separator="/",
            asset_aliases_to_hb=_KRAKEN_TO_HB,
            asset_aliases_from_hb=_KRAKEN_FROM_HB,
        )

    def test_from_exchange_pair(self) -> None:
        assert self.mapper.from_exchange_pair("XBT/USD") == "BTC-USD"

    def test_to_exchange_pair(self) -> None:
        # _KRAKEN_FROM_HB maps USD→ZUSD, so BTC-USD → XBT/ZUSD
        assert self.mapper.to_exchange_pair("BTC-USD") == "XBT/ZUSD"

    def test_unknown_pair_bad_sep_raises(self) -> None:
        """No slash present → split gives single chunk → UnknownPairError."""
        with pytest.raises(UnknownPairError):
            self.mapper.from_exchange_pair("BTCUSD")

    def test_round_trip_separator(self) -> None:
        # Use a pair that survives the round-trip through Kraken aliases
        exch = "XBT/ZUSD"
        assert self.mapper.to_exchange_pair(self.mapper.from_exchange_pair(exch)) == exch


# ---------------------------------------------------------------------------
# 5. TestFallbackLookup
# ---------------------------------------------------------------------------


class TestFallbackLookup:
    def test_fallback_called_on_unknown(self) -> None:
        fallback = MagicMock(return_value="BTC-USD")
        mapper = RuleBasedMapper(
            separator="-",
            fallback_lookup=fallback,
        )
        # "BTCUSD" has no dash so split returns 1 part → triggers fallback
        result = mapper.from_exchange_pair("BTCUSD")
        fallback.assert_called_once_with("BTCUSD")
        assert result == "BTC-USD"

    def test_fallback_none_raises(self) -> None:
        fallback = MagicMock(return_value=None)
        mapper = RuleBasedMapper(
            separator="-",
            fallback_lookup=fallback,
        )
        with pytest.raises(UnknownPairError):
            mapper.from_exchange_pair("BTCUSD")

    def test_no_fallback_raises_directly(self) -> None:
        mapper = RuleBasedMapper(separator="-")
        with pytest.raises(UnknownPairError):
            mapper.from_exchange_pair("BTCUSD")


# ---------------------------------------------------------------------------
# 6. TestRoundTripIdentity  (parametrized, no hypothesis)
# ---------------------------------------------------------------------------

_IDENTITY_PAIRS = [
    "BTC-USD",
    "ETH-USDT",
    "SOL-USDC",
    "ADA-BTC",
    "DOT-ETH",
]

_BINANCE_PAIRS_HB = [
    "BTC-USDT",
    "ETH-USDT",
    "SOL-USDT",
    "BNB-USDT",
    "ADA-USDT",
]

_BINANCE_MAPPER = RuleBasedMapper(
    separator=None,
    known_quote_assets=("USDT", "USDC", "BTC", "ETH", "BNB"),
)


class TestRoundTripIdentity:
    @pytest.mark.parametrize("hb_pair", _IDENTITY_PAIRS)
    def test_identity_mapper_from_to(self, hb_pair: str) -> None:
        mapper = IdentityMapper()
        exch = mapper.to_exchange_pair(hb_pair)
        assert mapper.from_exchange_pair(exch) == hb_pair

    @pytest.mark.parametrize("hb_pair", _BINANCE_PAIRS_HB)
    def test_rule_based_mapper_round_trip(self, hb_pair: str) -> None:
        exch = _BINANCE_MAPPER.to_exchange_pair(hb_pair)
        assert _BINANCE_MAPPER.from_exchange_pair(exch) == hb_pair


# ---------------------------------------------------------------------------
# 7. TestUnknownPairError
# ---------------------------------------------------------------------------


class TestUnknownPairError:
    def test_from_exchange_pair_missing_sep(self) -> None:
        mapper = RuleBasedMapper(separator="-")
        with pytest.raises(UnknownPairError, match="Cannot map pair"):
            mapper.from_exchange_pair("BAD")

    def test_from_exchange_pair_no_sep_bad_quote(self) -> None:
        mapper = RuleBasedMapper(separator=None, known_quote_assets=("USDT",))
        with pytest.raises(UnknownPairError):
            mapper.from_exchange_pair("BTCBADQUOTE")

    def test_to_exchange_pair_no_dash(self) -> None:
        mapper = RuleBasedMapper(separator=None, known_quote_assets=("USDT",))
        with pytest.raises(UnknownPairError, match="Cannot map pair"):
            mapper.to_exchange_pair("ONLYONE")

    def test_error_contains_offending_pair(self) -> None:
        mapper = RuleBasedMapper(separator="-")
        exc = pytest.raises(UnknownPairError, mapper.from_exchange_pair, "BAD")
        assert exc.value.pair == "BAD"
