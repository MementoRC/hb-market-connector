"""Coinbase ``client_order_id`` codec for linked-order (group, leg) identifiers.

Format: ``mcl-<group_id>-<leg_index>`` with canonical decimal integers (no
leading zeros, ASCII digits only), so the mapping is bijective. ``group_id`` is
any non-negative int (unbounded); ``leg_index`` is in ``0..15``.

Foreign ids, such as the default ``coinbase-<uuid>`` ids or any other prefix,
decode to ``None``.

Satisfies hb-strategy-framework's structural ``LinkedOrderIdCodec`` Protocol
without importing it (ADR 0001: no L2->L2 imports).
"""

from __future__ import annotations

import re

__all__ = ["CoinbaseClientOrderIdLinkCodec"]

_TAG_RE = re.compile(r"mcl-(0|[1-9][0-9]*)-(0|[1-9][0-9]*)")


class CoinbaseClientOrderIdLinkCodec:
    """Encode/decode (group_id, leg_index) as ``mcl-<group_id>-<leg_index>``.

    Canonical decimal makes encode/decode bijective. ``coinbase-<uuid>``
    default ids and other prefixes decode to ``None``.
    """

    LEG_INDEX_MAX = 15
    PREFIX = "mcl-"

    def encode(self, group_id: int, leg_index: int) -> str:
        for name, value in (("group_id", group_id), ("leg_index", leg_index)):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be int, got {type(value).__name__}")
        if group_id < 0:
            raise ValueError(f"group_id {group_id} must be >= 0")
        if not 0 <= leg_index <= self.LEG_INDEX_MAX:
            raise ValueError(f"leg_index {leg_index} out of range [0, {self.LEG_INDEX_MAX}]")
        return f"{self.PREFIX}{group_id}-{leg_index}"

    def decode(self, tag: str | int) -> tuple[int, int] | None:
        if not isinstance(tag, str):
            return None
        match = _TAG_RE.fullmatch(tag)
        if match is None:
            return None
        leg = int(match.group(2))
        if leg > self.LEG_INDEX_MAX:
            return None
        return int(match.group(1)), leg
