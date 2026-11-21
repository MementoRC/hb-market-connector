"""Shared ``mcl-<group_id>-<leg_index>`` tag codec for linked-order identifiers.

Venue codecs subclass :class:`PrefixedLinkIdCodec` and set ``GROUP_ID_MAX``.
Canonical decimal integers (no leading zeros, ASCII digits only) make the
mapping bijective. Satisfies hb-strategy-framework's structural
``LinkedOrderIdCodec`` Protocol without importing it (ADR 0001: no L2->L2
imports).
"""

from __future__ import annotations

import re

__all__ = ["PrefixedLinkIdCodec"]

_TAG_RE = re.compile(r"mcl-(0|[1-9][0-9]*)-(0|[1-9][0-9]*)")


class PrefixedLinkIdCodec:
    """Encode/decode (group_id, leg_index) as ``mcl-<group_id>-<leg_index>``.

    ``GROUP_ID_MAX`` of ``None`` means unbounded. Foreign tags (other prefixes,
    non-str values, malformed or out-of-range numbers) decode to ``None``.
    """

    PREFIX = "mcl-"
    LEG_INDEX_MAX = 15
    GROUP_ID_MAX: int | None = None

    def encode(self, group_id: int, leg_index: int) -> str:
        for name, value in (("group_id", group_id), ("leg_index", leg_index)):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be int, got {type(value).__name__}")
        if group_id < 0 or (self.GROUP_ID_MAX is not None and group_id > self.GROUP_ID_MAX):
            upper = "inf" if self.GROUP_ID_MAX is None else str(self.GROUP_ID_MAX)
            raise ValueError(f"group_id {group_id} out of range [0, {upper}]")
        if not 0 <= leg_index <= self.LEG_INDEX_MAX:
            raise ValueError(f"leg_index {leg_index} out of range [0, {self.LEG_INDEX_MAX}]")
        return f"{self.PREFIX}{group_id}-{leg_index}"

    def decode(self, tag: str | int) -> tuple[int, int] | None:
        if not isinstance(tag, str):
            return None
        match = _TAG_RE.fullmatch(tag)
        if match is None:
            return None
        group_id, leg = int(match.group(1)), int(match.group(2))
        if leg > self.LEG_INDEX_MAX:
            return None
        if self.GROUP_ID_MAX is not None and group_id > self.GROUP_ID_MAX:
            return None
        return group_id, leg
