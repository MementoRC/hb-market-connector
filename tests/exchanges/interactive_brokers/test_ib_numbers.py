"""Unit tests for the shared IB number parsers in _ib_numbers (#181)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from market_connector.exchanges.interactive_brokers._ib_numbers import (
    UNSET_DOUBLE,
    decimal_from_str_or_none,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10500.75", Decimal("10500.75")),
        (" 5 ", Decimal(5)),
        ("0", Decimal(0)),
        ("-12.5", Decimal("-12.5")),
        ("0.1000000000000000055511", Decimal("0.1000000000000000055511")),
    ],
)
def test_parses_exact_decimal(raw: str, expected: Decimal) -> None:
    assert decimal_from_str_or_none(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [None, "", "  ", "abc", "nan", "NaN", "inf", repr(UNSET_DOUBLE), 5, 1.5, UNSET_DOUBLE],
)
def test_unset_or_invalid_is_none(raw: object) -> None:
    assert decimal_from_str_or_none(raw) is None
