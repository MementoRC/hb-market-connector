"""Interactive Brokers ``Order.orderRef`` codec for linked-order (group, leg) identifiers.

Format: ``mcl-<group_id>-<leg_index>`` with canonical decimal integers, carried in the
free-text ``orderRef`` field. ``group_id`` is any non-negative int (unbounded);
``leg_index`` is in ``0..15``. Foreign or empty refs decode to ``None``.

Satisfies hb-strategy-framework's structural ``LinkedOrderIdCodec`` Protocol without
importing it (ADR 0001: no L2->L2 imports).
"""

from __future__ import annotations

from market_connector.exchanges._prefixed_link_codec import PrefixedLinkIdCodec

__all__ = ["IbOrderRefLinkCodec"]


class IbOrderRefLinkCodec(PrefixedLinkIdCodec):
    """Encode/decode (group_id, leg_index) as ``mcl-<group_id>-<leg_index>`` in ``orderRef``."""

    GROUP_ID_MAX = None
