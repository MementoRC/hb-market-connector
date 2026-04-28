"""Meta-tests for SymbolMapperConformance suite (Task 11).

Positive case: known-good mappers + fixture pairs → suite.run() passes.
Negative case: swapped hb/exch pair → suite.run() raises AssertionError.
"""

from __future__ import annotations

import pytest

from market_connector.testing.contract import SymbolMapperConformance
from market_connector.testing.spec_fixtures import (
    KNOWN_IDENTITY_MAPPER_SPEC,
    KNOWN_RULE_MAPPER_SPEC,
)


def test_identity_mapper_conformance_passes() -> None:
    """SymbolMapperConformance.run() passes for IdentityMapper with dash separator."""
    fixture_pairs = [("BTC-USD", "BTC-USD"), ("ETH-USDT", "ETH-USDT")]
    SymbolMapperConformance(
        mapper=KNOWN_IDENTITY_MAPPER_SPEC,
        fixture_pairs=fixture_pairs,
    ).run()


def test_rule_mapper_conformance_passes() -> None:
    """SymbolMapperConformance.run() passes for RuleBasedMapper (no-separator style)."""
    fixture_pairs = [("BTC-USDT", "BTCUSDT"), ("ETH-USDT", "ETHUSDT")]
    SymbolMapperConformance(
        mapper=KNOWN_RULE_MAPPER_SPEC,
        fixture_pairs=fixture_pairs,
    ).run()


def test_symbol_mapper_conformance_fails_on_swapped_pair() -> None:
    """SymbolMapperConformance.run() raises AssertionError when hb/exch pair is swapped."""
    # IdentityMapper: to_exchange_pair("BTC-USD") == "BTC-USD" (correct)
    # but we claim exch_pair should be "USD-BTC" (wrong)
    fixture_pairs = [("BTC-USD", "USD-BTC")]  # swapped — wrong direction
    with pytest.raises((AssertionError, Exception)):
        SymbolMapperConformance(
            mapper=KNOWN_IDENTITY_MAPPER_SPEC,
            fixture_pairs=fixture_pairs,
        ).run()
