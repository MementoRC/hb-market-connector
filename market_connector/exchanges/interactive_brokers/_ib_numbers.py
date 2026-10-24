"""Shared IB number conversion: floats to exact Decimal, mapping "not provided" to None."""

from __future__ import annotations

import math
import sys
from decimal import Decimal
from typing import Any

UNSET_DOUBLE = sys.float_info.max


def decimal_or_none(value: Any) -> Decimal | None:
    """Convert an IB float; None when None, NaN or the unset sentinel. Zero is a real value."""
    if value is None or value == UNSET_DOUBLE or math.isnan(value):
        return None
    return Decimal(repr(float(value)))
