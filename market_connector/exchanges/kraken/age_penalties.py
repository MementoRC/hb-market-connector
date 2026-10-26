"""Kraken order-age matching penalties.

Kraken's matching-engine counter charges amend/edit/cancel more the younger the
order is (https://docs.kraken.com/api/docs/guides/spot-ratelimits). Each function
returns the TOTAL matching units for a call (fixed cost plus age penalty); a
cancel of an order 300s or older costs 0.
"""

from __future__ import annotations

# (exclusive upper age bound in seconds, total units); the last row is open-ended.
_CANCEL: tuple[tuple[float, int], ...] = (
    (5, 8),
    (10, 6),
    (15, 5),
    (45, 4),
    (90, 2),
    (300, 1),
)
_AMEND: tuple[tuple[float, int], ...] = ((5, 4), (10, 3), (15, 2))
_EDIT: tuple[tuple[float, int], ...] = ((5, 7), (10, 6), (15, 5), (45, 3), (90, 2))


def _lookup(table: tuple[tuple[float, int], ...], age_s: float, older: int) -> int:
    for bound, units in table:
        if age_s < bound:
            return units
    return older


def cancel_penalty(age_s: float) -> int:
    """Matching-pool units a CancelOrder costs for an order ``age_s`` seconds old."""
    return _lookup(_CANCEL, age_s, 0)


def amend_penalty(age_s: float) -> int:
    """Matching-pool units an AmendOrder costs (fixed 1 plus age penalty)."""
    return _lookup(_AMEND, age_s, 1)


def edit_penalty(age_s: float) -> int:
    """Matching-pool units an EditOrder costs (fixed 1 plus age penalty)."""
    return _lookup(_EDIT, age_s, 1)
