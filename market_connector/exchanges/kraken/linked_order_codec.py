"""Kraken ``userref`` codec for linked-order (group, leg) identifiers.

Bit budget (negative int32 half only; Kraken ``userref`` is a signed int32)::

    1 bit sign (always 1) | 4 bits marker (0xA) | 23 bits group id | 4 bits leg index

The tag is the bitwise complement of ``(marker << 27) | (group_id << 4) | leg``,
so it is always in ``[-2**31, -1]``.

Why negative: the positive half ``1..2**31-1`` is used by the connector's
existing per-order userref counter (``hb_compat/kraken_connector_core.py``
``_assign_userref``). Staying negative keeps the two ranges disjoint, and the
marker nibble makes foreign userrefs (manual orders, other bots) decode to
``None``, which is what strategy-framework orphan-cancel safety relies on.

Capacity hard stop: strategy-framework's group counter is monotonic. Once it
passes ``GROUP_ID_MAX`` every new linked group fails SF's pre-place round-trip
(``LinkIdCodecError``), permanently for that account until SF id reuse lands
(tracked as an SF follow-up).

Uniqueness caveat: Kraken does not enforce userref uniqueness. If group ids are
ever reused, a decoded tag is authoritative only for OPEN orders and for closed
orders newer than the group's creation. Closed-order lookups by userref may
return a previous group's terminal legs.

Satisfies hb-strategy-framework's structural ``LinkedOrderIdCodec`` Protocol
without importing it (ADR 0001: no L2->L2 imports).
"""

from __future__ import annotations

__all__ = ["KrakenUserrefLinkCodec"]

_MARKER = 0xA
_MARKER_SHIFT = 27
_LEG_BITS = 4
_INT32_MIN = -(2**31)


class KrakenUserrefLinkCodec:
    """Encode/decode (group_id, leg_index) as a negative int32 Kraken userref.

    Layout: 1 sign / 4 marker (0xA) / 23 group id / 4 leg index, negative half
    only. The positive half belongs to the connector's per-order userref
    counter; the marker makes foreign userrefs decode to ``None``.

    Capacity: once SF's monotonic group counter exceeds ``GROUP_ID_MAX``, every
    new linked group fails SF's pre-place round-trip (``LinkIdCodecError``)
    until SF id reuse lands. Kraken does not enforce userref uniqueness, so
    with reused group ids a decoded tag is authoritative only for open orders
    and closed orders newer than the group's creation.
    """

    GROUP_ID_MAX = 2**23 - 1
    LEG_INDEX_MAX = 15

    def encode(self, group_id: int, leg_index: int) -> int:
        for name, value in (("group_id", group_id), ("leg_index", leg_index)):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be int, got {type(value).__name__}")
        if not 0 <= group_id <= self.GROUP_ID_MAX:
            raise ValueError(f"group_id {group_id} out of range [0, {self.GROUP_ID_MAX}]")
        if not 0 <= leg_index <= self.LEG_INDEX_MAX:
            raise ValueError(f"leg_index {leg_index} out of range [0, {self.LEG_INDEX_MAX}]")
        return ~((_MARKER << _MARKER_SHIFT) | (group_id << _LEG_BITS) | leg_index)

    def decode(self, tag: str | int) -> tuple[int, int] | None:
        if isinstance(tag, bool) or not isinstance(tag, int):
            return None
        if not _INT32_MIN <= tag <= -1:
            return None
        raw = ~tag
        if raw >> _MARKER_SHIFT != _MARKER:
            return None
        return (raw >> _LEG_BITS) & self.GROUP_ID_MAX, raw & self.LEG_INDEX_MAX
