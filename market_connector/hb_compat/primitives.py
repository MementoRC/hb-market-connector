"""Adapter binding gateway-framework OrderType/TradeType to the canonical
hb-data-type-primitives enums (ADR 0001 Group D).

``market_connector/primitives.py`` needs ``StrEnum`` members — wire-format
string serialization for exchange gateway payloads (JSON request bodies,
converters). ``hb-data-type-primitives``' ``OrderType``/``TradeType`` are
int-valued ``Enum``s shared across the sub-package dependency graph and are
not str-serializable in the shape this package's converters/quantize logic
expects, so a straight type-swap is not possible.

This module keeps the local ``StrEnum`` shape (preserving existing
serialization/quantize behavior) while asserting, at import time, that every
local member name is backed by an identically-named canonical member — so
drift in the canonical source is caught immediately instead of silently
diverging. ``market_connector/primitives.py`` imports its ``OrderType``/
``TradeType`` from here rather than redeclaring them.

Distinct from ``market_connector/hb_compat/common.py`` (ADR 0001 Group A3),
which re-exports the *parent hummingbot* ``OrderType``/``TradeType`` for
``live_market_access`` — a different consumer with a different canonical
source (``hummingbot.core.data_type.common``, not ``data_type_primitives``).
"""

from enum import StrEnum

from data_type_primitives.common import OrderType as CanonicalOrderType
from data_type_primitives.common import TradeType as CanonicalTradeType


class _StrValue(StrEnum):
    """Base mixin: str(member) returns the value."""


class OrderType(_StrValue):
    """Order type for gateway execution methods.

    Members mirror a subset of ``CanonicalOrderType`` by name; values remain
    plain strings for exchange-gateway wire serialization.
    """

    LIMIT = "LIMIT"
    MARKET = "MARKET"
    LIMIT_MAKER = "LIMIT_MAKER"


class TradeType(_StrValue):
    """Trade side for gateway execution methods.

    Members mirror a subset of ``CanonicalTradeType`` by name; values remain
    plain strings for exchange-gateway wire serialization.
    """

    BUY = "BUY"
    SELL = "SELL"


def _assert_members_match_canonical() -> None:
    """Fail fast at import time if local members drift from the canonical enums."""
    stale_order_members = {member.name for member in OrderType} - {
        member.name for member in CanonicalOrderType
    }
    if stale_order_members:
        raise AssertionError(
            f"market_connector OrderType members {stale_order_members} have no "
            "matching CanonicalOrderType (data_type_primitives) member"
        )

    stale_trade_members = {member.name for member in TradeType} - {
        member.name for member in CanonicalTradeType
    }
    if stale_trade_members:
        raise AssertionError(
            f"market_connector TradeType members {stale_trade_members} have no "
            "matching CanonicalTradeType (data_type_primitives) member"
        )


_assert_members_match_canonical()

__all__ = ["CanonicalOrderType", "CanonicalTradeType", "OrderType", "TradeType"]
