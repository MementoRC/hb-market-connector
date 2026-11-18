"""Kraken ``cl_ord_id`` codec for linked-order (group, leg) identifiers.

Format: ``mcl-<group_id>-<leg_index>`` (see ``PrefixedLinkIdCodec``).

Carrier: Kraken AddOrder ``cl_ord_id``, free-format ASCII up to 18 characters,
unique among OPEN orders per client. It is used instead of ``userref`` because
negative userrefs are undocumented and the positive half is the connector's
per-order userref counter (``hb_compat/kraken_connector_core.py``
``_assign_userref``).

``cl_ord_id`` is MUTUALLY EXCLUSIVE with ``userref`` on AddOrder, so linked legs
carry no userref. Placement wiring and lost-create recovery keyed on
``cl_ord_id`` are tracked in hb-market-connector#255. Non-linked orders keep the
userref path.

Capacity: the 18-character cap allows ``4 + 11 + 1 + 2`` characters, so
``GROUP_ID_MAX`` is 99_999_999_999. ``encode`` raises ``ValueError`` above it
before any request goes out (strategy-framework's pre-place round-trip turns it
into ``LinkIdCodecError``). SF's group counter is monotonic, so this is a hard
ceiling (about 10^11) unless SF adds id reuse.

Uniqueness: Kraken requires uniqueness only among open orders. If group ids are
ever reused, a decoded tag is authoritative only for OPEN orders and for closed
orders newer than the group's creation.

Foreign ``cl_ord_id`` values fail the prefix filter and decode to ``None``,
which strategy-framework orphan-cancel safety relies on.
"""

from __future__ import annotations

from market_connector.exchanges._prefixed_link_codec import PrefixedLinkIdCodec

__all__ = ["CORE_USERREF_MAX", "KrakenClOrdIdLinkCodec", "KrakenUserrefLinkCodec"]

# Highest userref the connector core's per-order counter may assign. Everything above it
# (bit 30 set) belongs to ``KrakenUserrefLinkCodec``, so the two ranges never collide.
CORE_USERREF_MAX = 2**30 - 1


class KrakenClOrdIdLinkCodec(PrefixedLinkIdCodec):
    """Encode/decode (group_id, leg_index) as a Kraken ``cl_ord_id`` (<= 18 chars)."""

    GROUP_ID_MAX = 99_999_999_999


class KrakenUserrefLinkCodec:
    """Encode/decode (group_id, leg_index) as a Kraken int32 ``userref``.

    Layout (31 bits, always positive): bit 30 is a link marker, bits 4..29 hold the group
    id (high), bits 0..3 the leg index (low). ``userref`` and ``cl_ord_id`` are mutually
    exclusive on AddOrder, so a bracket entry carries a userref tag and lost-reply
    recovery runs through the existing userref resolver (``get_open_orders(userref=...)``).

    The connector core's per-order counter stays below the marker bit
    (``CORE_USERREF_MAX``), so its userrefs, 0, negatives and anything out of int32 range
    are foreign and decode to ``None``.

    Capacity: ``GROUP_ID_MAX`` is 2**26 - 1 (about 6.7e7 groups); ``encode`` raises
    ``ValueError`` above it. Satisfies strategy-framework's ``LinkedOrderIdCodec`` and
    ``BoundedLinkIdCodec`` (``max_group_id``).
    """

    LEG_INDEX_BITS = 4
    LEG_INDEX_MAX = (1 << LEG_INDEX_BITS) - 1
    GROUP_ID_MAX = (1 << 26) - 1
    _MARKER = 1 << 30
    _INT32_MAX = 2**31 - 1

    @property
    def max_group_id(self) -> int:
        """The largest group id ``encode`` accepts."""
        return self.GROUP_ID_MAX

    def encode(self, group_id: int, leg_index: int) -> int:
        """Return the int32 userref for ``(group_id, leg_index)``.

        Raises:
            TypeError: If either argument is not a plain ``int`` (``bool`` included).
            ValueError: If either is outside its range (never wrapped or clamped).
        """
        for name, value in (("group_id", group_id), ("leg_index", leg_index)):
            if type(value) is not int:
                raise TypeError(f"{name} must be an int, got {type(value).__name__}")
        if not 0 <= group_id <= self.GROUP_ID_MAX:
            raise ValueError(f"group_id {group_id} outside 0..{self.GROUP_ID_MAX}")
        if not 0 <= leg_index <= self.LEG_INDEX_MAX:
            raise ValueError(f"leg_index {leg_index} outside 0..{self.LEG_INDEX_MAX}")
        return self._MARKER | (group_id << self.LEG_INDEX_BITS) | leg_index

    def decode(self, tag: str | int) -> tuple[int, int] | None:
        """Return ``(group_id, leg_index)``, or ``None`` for a foreign tag."""
        if type(tag) is not int or not self._MARKER <= tag <= self._INT32_MAX:
            return None
        body = tag & ~self._MARKER
        return body >> self.LEG_INDEX_BITS, body & self.LEG_INDEX_MAX
