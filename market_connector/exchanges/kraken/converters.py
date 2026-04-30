"""Order-type translation utilities for the Kraken adapter layer.

This module is the adapter-layer bridge for the conditional ``OrderType``
extension from ``_for_bleed/add-new-order-types``.  The gateway core
(``OrdersMixin``) handles only ``LIMIT`` and ``MARKET``; conditional order
types (``STOP_LOSS``, ``TAKE_PROFIT``, ``TRAILING_STOP``) are mapped here
in the hb_compat layer.

Notes:
    - If ``hummingbot.core.data_type.common_types.OrderType`` is not available
      (hb-market-connector used standalone), the converter accepts string and
      Enum-like duck-typed inputs via the ``name`` attribute.
    - Native Kraken callers may pass Kraken-native strings directly; they are
      returned as-is after lowercase normalisation.
    - The converter does NOT validate order parameters (e.g. stop price for
      ``STOP_LOSS``).  That is the bridge's responsibility.

Examples:
    >>> kraken_ordertype_from_hb("LIMIT")
    'limit'
    >>> kraken_ordertype_from_hb("market")
    'market'
    >>> # With hummingbot OrderType enum available:
    >>> # from hummingbot.core.data_type.common_types import OrderType
    >>> # kraken_ordertype_from_hb(OrderType.LIMIT)  -> 'limit'
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Canonical mapping (covers both hummingbot primitives and string inputs)
# ---------------------------------------------------------------------------

_HB_TO_KRAKEN: dict[str, str] = {
    "LIMIT": "limit",
    "MARKET": "market",
    # Conditional types — only reachable when _for_bleed/add-new-order-types is integrated
    "STOP_LOSS": "stop-loss",
    "TAKE_PROFIT": "take-profit",
    "TRAILING_STOP": "trailing-stop",
    # Limit-maker passthrough (hb-market-connector primitives)
    "LIMIT_MAKER": "limit",
}


def kraken_ordertype_from_hb(
    order_type: Any,
    trigger_kind: str | None = None,  # noqa: ARG001 — reserved for future qualification
) -> str:
    """Translate a hummingbot ``OrderType`` value to a Kraken order-type string.

    Accepts:
    - ``market_connector.primitives.OrderType`` members (``LIMIT``, ``MARKET``, ``LIMIT_MAKER``)
    - hummingbot ``OrderType`` members with conditional values when available
    - plain strings (case-insensitive; returned normalised to lowercase)
    - any Enum-like object exposing a ``.name`` attribute

    Args:
        order_type: Order type to translate.
        trigger_kind: Reserved for future use (secondary qualifier for conditional
            orders, e.g. ``"limit"`` vs ``"market"`` for stop-loss exit type).
            Currently ignored.

    Returns:
        Kraken order-type string, e.g. ``"limit"``, ``"market"``,
        ``"stop-loss"``, ``"take-profit"``, ``"trailing-stop"``.

    Raises:
        ValueError: If the input cannot be mapped to a known Kraken order type.

    Examples:
        >>> kraken_ordertype_from_hb("LIMIT")
        'limit'
        >>> kraken_ordertype_from_hb("stop_loss")
        Traceback (most recent call last):
            ...
        ValueError: ...
    """
    # Resolve to an uppercase key
    if isinstance(order_type, str):
        key = order_type.upper().replace("-", "_")
    elif hasattr(order_type, "name"):
        # Handles any StrEnum / Enum (including hummingbot OrderType)
        key = str(order_type.name).upper()
    else:
        key = str(order_type).upper()

    result = _HB_TO_KRAKEN.get(key)
    if result is None:
        raise ValueError(
            f"Cannot map order type {order_type!r} to a Kraken order-type string. "
            f"Known types: {sorted(_HB_TO_KRAKEN)}"
        )
    return result


__all__ = [
    "kraken_ordertype_from_hb",
]
