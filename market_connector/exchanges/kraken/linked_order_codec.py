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

__all__ = ["KrakenClOrdIdLinkCodec", "KrakenUserrefLinkCodec"]


class KrakenClOrdIdLinkCodec(PrefixedLinkIdCodec):
    """Encode/decode (group_id, leg_index) as a Kraken ``cl_ord_id`` (<= 18 chars)."""

    GROUP_ID_MAX: int = 99_999_999_999

    @property
    def max_group_id(self) -> int:
        """Largest encodable group id (``GROUP_ID_MAX``)."""
        return self.GROUP_ID_MAX


class KrakenUserrefLinkCodec:
    """Encode/decode (group_id, leg_index) as a Kraken int32 ``userref``.

    Layout: ``-(1 + ((group_id << 4) | leg_index))``, a negative int32 (-2**31..-1). The
    group id (27 bits) is high, the leg index (4 bits) low. The codec uses negative space
    because the connector core's per-order counter only ever produces non-negative
    userrefs, so the two never collide, and Kraken accepts and echoes negative userrefs
    (live-verified 2026-11-18). ``userref`` and ``cl_ord_id`` are mutually exclusive on
    AddOrder, so a bracket entry carries a userref tag and lost-reply recovery runs
    through the existing userref resolver (``get_open_orders(userref=...)``).

    Every value >= 0 and anything out of int32 range is foreign and decodes to ``None``.

    Capacity: ``GROUP_ID_MAX`` is 2**27 - 1 (about 1.3e8 groups); ``encode`` raises
    ``ValueError`` above it. Satisfies strategy-framework's ``LinkedOrderIdCodec`` and
    ``BoundedLinkIdCodec`` (``max_group_id``).
    """

    LEG_BITS = 4
    LEG_INDEX_MAX = (1 << LEG_BITS) - 1
    GROUP_ID_MAX = (1 << 27) - 1
    _INT32_MIN = -(2**31)

    @property
    def max_group_id(self) -> int:
        """The largest group id ``encode`` accepts."""
        return self.GROUP_ID_MAX

    def encode(self, group_id: int, leg_index: int) -> int:
        """Return the int32 userref for ``(group_id, leg_index)``.

        Raises:
            ValueError: If either argument is not a plain ``int`` (``bool`` included) or is
                outside its range (never wrapped or clamped).
        """
        for name, value in (("group_id", group_id), ("leg_index", leg_index)):
            if type(value) is not int:
                raise ValueError(f"{name} must be an int, got {type(value).__name__}")
        if not 0 <= group_id <= self.GROUP_ID_MAX:
            raise ValueError(f"group_id {group_id} outside 0..{self.GROUP_ID_MAX}")
        if not 0 <= leg_index <= self.LEG_INDEX_MAX:
            raise ValueError(f"leg_index {leg_index} outside 0..{self.LEG_INDEX_MAX}")
        return -(1 + ((group_id << self.LEG_BITS) | leg_index))

    def decode(self, tag: str | int) -> tuple[int, int] | None:
        """Return ``(group_id, leg_index)``, or ``None`` for a foreign tag."""
        if type(tag) is not int or not self._INT32_MIN <= tag < 0:
            return None
        body = -tag - 1
        return body >> self.LEG_BITS, body & self.LEG_INDEX_MAX
