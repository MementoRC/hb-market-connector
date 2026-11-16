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

__all__ = ["KrakenClOrdIdLinkCodec"]


class KrakenClOrdIdLinkCodec(PrefixedLinkIdCodec):
    """Encode/decode (group_id, leg_index) as a Kraken ``cl_ord_id`` (<= 18 chars)."""

    GROUP_ID_MAX = 99_999_999_999
