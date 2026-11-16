"""Coinbase ``client_order_id`` codec for linked-order (group, leg) identifiers.

Format: ``mcl-<group_id>-<leg_index>`` with canonical decimal integers (no
leading zeros, ASCII digits only), so the mapping is bijective. ``group_id`` is
any non-negative int (unbounded); ``leg_index`` is in ``0..15``.

Foreign ids, such as the default ``coinbase-<uuid>`` ids or any other prefix,
decode to ``None``.

Coinbase dedupes ``client_order_id``: "If the ID provided is not unique, the
order will not be created and the order corresponding with that ID will be
returned instead", with no documented time window. Group ids must therefore
never be reissued on Coinbase.

Satisfies hb-strategy-framework's structural ``LinkedOrderIdCodec`` Protocol
without importing it (ADR 0001: no L2->L2 imports).
"""

from __future__ import annotations

from market_connector.exchanges._prefixed_link_codec import PrefixedLinkIdCodec

__all__ = ["CoinbaseClientOrderIdLinkCodec"]


class CoinbaseClientOrderIdLinkCodec(PrefixedLinkIdCodec):
    """Encode/decode (group_id, leg_index) as ``mcl-<group_id>-<leg_index>``.

    Canonical decimal makes encode/decode bijective. ``coinbase-<uuid>``
    default ids and other prefixes decode to ``None``.
    """

    GROUP_ID_MAX = None
