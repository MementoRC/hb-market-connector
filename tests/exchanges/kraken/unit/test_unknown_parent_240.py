"""Issue #240 criterion 5: an UNKNOWN bracket parent is adopted/failed by userref like any order.

A bracket parent is a plain AddOrder carrying a codec-encoded int32 userref, so the existing
``KrakenConnectorCore._resolve_unknown_orders`` must resolve it unchanged. These are parity
anchors: they pass at base 8a217b3 and must keep passing once the codec is wired in. They
use a literal userref (not the codec) so they do not depend on the unimplemented class.
"""

from __future__ import annotations

from typing import Any

from tests.exchanges.kraken.unit import test_kraken_connector_core_211 as base
from tests.exchanges.kraken.unit import test_userref_recovery_214 as rec

# Fixtures reused as-is (pytest discovers them by module attribute).
bare_connector, bus, connector = base.bare_connector, base.bus, base.connector
gateway, seen, rest = base.gateway, base.seen, rec.rest

_BRACKET_PARENT_USERREF = -(1 + (41 << 4 | 0))  # negative int32 tag: group 41, leg 0


class TestUnknownBracketParentResolution:
    async def test_parent_found_in_open_orders_is_adopted(
        self, connector: Any, rest: Any, seen: Any
    ) -> None:
        cid = await rec._lose_one(connector, rest, rec._LOST[0], lost_as=None)  # noqa: SLF001
        connector._userrefs[cid] = _BRACKET_PARENT_USERREF  # noqa: SLF001
        txid = rest.register_on_exchange(_BRACKET_PARENT_USERREF, "open")

        await connector._resolve_unknown_orders()  # noqa: SLF001

        assert connector.fetch_order(cid).exchange_order_id == txid
        assert seen.of(base.FAILURE) == []

    async def test_parent_found_in_closed_orders_is_adopted(
        self, connector: Any, rest: Any, seen: Any
    ) -> None:
        cid = await rec._lose_one(connector, rest, rec._LOST[0], lost_as=None)  # noqa: SLF001
        connector._userrefs[cid] = _BRACKET_PARENT_USERREF  # noqa: SLF001
        txid = rest.register_on_exchange(_BRACKET_PARENT_USERREF, "closed")

        await connector._resolve_unknown_orders()  # noqa: SLF001

        assert connector.fetch_order(cid).exchange_order_id == txid
        assert seen.of(base.FAILURE) == []

    async def test_parent_absent_is_failed_once(self, connector: Any, rest: Any, seen: Any) -> None:
        cid = await rec._lose_one(connector, rest, rec._LOST[0], lost_as=None)  # noqa: SLF001
        connector._userrefs[cid] = _BRACKET_PARENT_USERREF  # noqa: SLF001

        await connector._resolve_unknown_orders()  # noqa: SLF001
        await connector._resolve_unknown_orders()  # noqa: SLF001

        assert cid not in connector.in_flight_orders
        assert len(seen.of(base.FAILURE)) == 1
