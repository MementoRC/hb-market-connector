"""Mapping from canonical interval labels to IB bar-size strings."""

from __future__ import annotations

_IB_BAR_SIZE: dict[str, str] = {
    "1m": "1 min",
    "5m": "5 mins",
    "15m": "15 mins",
    "30m": "30 mins",
    "1h": "1 hour",
    "4h": "4 hours",
    "1d": "1 day",
}


class InvalidIntervalError(ValueError):
    """Raised when an interval string has no IB bar-size mapping."""


def to_ib_bar_size(interval: str) -> str:
    """Translate a canonical interval string to an IB reqHistoricalData barSizeSetting.

    Supported: 1m, 5m, 15m, 30m, 1h, 4h, 1d.
    Raises InvalidIntervalError for any other value.
    """
    try:
        return _IB_BAR_SIZE[interval]
    except KeyError:
        raise InvalidIntervalError(
            f"Interval {interval!r} has no IB bar-size mapping. "
            f"Supported: {', '.join(_IB_BAR_SIZE)}"
        ) from None
