"""Shared IB number conversion: floats to exact Decimal, mapping "not provided" to None."""

from __future__ import annotations

import math
import sys
from decimal import Decimal, InvalidOperation
from typing import Any

UNSET_DOUBLE = sys.float_info.max

# IB serialises the sentinel via repr, which is just below the exact binary value.
_UNSET_STR_LIMIT = Decimal(repr(UNSET_DOUBLE))


def decimal_from_str_or_none(value: Any) -> Decimal | None:
    """Parse an IB numeric string; None when not a string, empty, unparsable, non-finite or unset.

    Strings at or above the UNSET_DOUBLE magnitude count as unset. Zero is a real value.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = Decimal(value.strip())
    except InvalidOperation:
        return None
    if not parsed.is_finite() or abs(parsed) >= _UNSET_STR_LIMIT:
        return None
    return parsed


def decimal_or_none(value: Any) -> Decimal | None:
    """Convert an IB float; None when None, NaN or the unset sentinel. Zero is a real value."""
    if value is None or value == UNSET_DOUBLE or math.isnan(value):
        return None
    return Decimal(repr(float(value)))
