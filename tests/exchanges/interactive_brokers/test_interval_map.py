"""Tests for IB bar-size interval mapping."""

from __future__ import annotations

import pytest

from market_connector.exchanges.interactive_brokers.interval_map import (
    InvalidIntervalError,
    to_ib_bar_size,
)


class TestToIbBarSize:
    @pytest.mark.parametrize(
        "interval,expected",
        [
            ("1m", "1 min"),
            ("5m", "5 mins"),
            ("15m", "15 mins"),
            ("30m", "30 mins"),
            ("1h", "1 hour"),
            ("4h", "4 hours"),
            ("1d", "1 day"),
        ],
    )
    def test_known_intervals_map_correctly(self, interval: str, expected: str) -> None:
        assert to_ib_bar_size(interval) == expected

    @pytest.mark.parametrize(
        "bad_interval",
        ["2m", "3h", "1w", "M1", "", "1M", "daily", "60m"],
    )
    def test_unknown_interval_raises_invalid_interval_error(self, bad_interval: str) -> None:
        with pytest.raises(InvalidIntervalError, match=bad_interval or "Interval"):
            to_ib_bar_size(bad_interval)

    def test_invalid_interval_error_is_value_error(self) -> None:
        with pytest.raises(ValueError):
            to_ib_bar_size("2m")
