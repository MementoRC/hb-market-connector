"""IB-specific per-order options, kept off the exchange-neutral HBOrder."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AdaptivePriority(StrEnum):
    """IB Adaptive algo ``adaptivePriority`` value."""

    PATIENT = "Patient"
    NORMAL = "Normal"
    URGENT = "Urgent"


@dataclass(frozen=True)
class IbOrderOptions:
    """IB-specific per-order options, kept off the exchange-neutral HBOrder.

    Everything is off by default. ``adaptive_priority`` routes the order through IB's
    Adaptive algo; it is only valid for SMART-routed STK MARKET/LIMIT orders.
    """

    adaptive_priority: AdaptivePriority | None = None
