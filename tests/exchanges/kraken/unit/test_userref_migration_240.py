"""Issue #240 review finding 1 (option A): core counter range restored, old orders stay plain.

The core's per-order userref counter has always produced NON-NEGATIVE userrefs; the link
codec now owns the negative int32 half. These tests pin that:

* the core counter is back to its original range (``_USERREF_MAX == 2**31 - 1``), so it can
  again hand out values above 2**30;
* no such value decodes as a linked leg;
* an order carrying an old-range userref (e.g. from the previous code) adopted through
  UNKNOWN resolution is a plain order, not a bracket leg;
* a negative userref (as Kraken returns it, JSON int) survives REST payload parsing.
"""

from __future__ import annotations

from typing import Any

import pytest

from market_connector.exchanges.kraken.hb_compat import kraken_connector_core as core_mod
from market_connector.exchanges.kraken.linked_order_codec import KrakenUserrefLinkCodec
from tests.exchanges.kraken.unit import test_kraken_connector_core_211 as base
from tests.exchanges.kraken.unit import test_userref_recovery_214 as rec
from tests.exchanges.kraken.unit import test_private_request_wire_215 as wire

# Fixtures reused as-is (pytest discovers them by module attribute).
bare_connector, bus, connector = base.bare_connector, base.bus, base.connector
gateway, seen, rest = base.gateway, base.seen, rec.rest

_OLD_RANGE_USERREF = 1_500_000_000  # > 2**30: legal for the old core, a bit-30 "leg" before
_LIVE_NEGATIVE_USERREF = -17  # live Kraken QueryOrders sample, 2026-11-18


class TestCoreCounterRangeRestored:
    def test_userref_max_is_original_int32_range(self) -> None:
        assert core_mod._USERREF_MAX == 2**31 - 1  # noqa: SLF001

    def test_counter_can_exceed_2_30(self, bare_connector: Any) -> None:
        bare_connector._userref_counter = 2**30 + 10  # noqa: SLF001
        bare_connector._assign_userref("cid-high")  # noqa: SLF001

        assert bare_connector._userrefs["cid-high"] > 2**30  # noqa: SLF001

    def test_counter_reaches_top_then_wraps_within_non_negative_int32(
        self, bare_connector: Any
    ) -> None:
        bare_connector._userref_counter = 2**31 - 3  # noqa: SLF001
        for i in range(4):
            bare_connector._assign_userref(f"cid-top-{i}")  # noqa: SLF001
        refs = list(bare_connector._userrefs.values())  # noqa: SLF001

        assert max(refs) > 2**30
        assert all(1 <= r <= 2**31 - 1 for r in refs)

    @pytest.mark.parametrize("start", [2**30 - 2, 2**30, 2**30 + 1, 2**31 - 3])
    def test_no_counter_value_decodes_as_linked(self, bare_connector: Any, start: int) -> None:
        codec = KrakenUserrefLinkCodec()
        bare_connector._userref_counter = start  # noqa: SLF001
        for i in range(4):
            bare_connector._assign_userref(f"cid-{start}-{i}")  # noqa: SLF001

        for userref in bare_connector._userrefs.values():  # noqa: SLF001
            assert codec.decode(userref) is None, userref


class TestOldRangeOrderAdoptedAsPlain:
    def test_old_range_userref_is_not_a_linked_leg(self) -> None:
        assert KrakenUserrefLinkCodec().decode(_OLD_RANGE_USERREF) is None

    @pytest.mark.parametrize("where", ["open", "closed"])
    async def test_unknown_order_with_old_range_userref_is_adopted_as_plain(
        self, connector: Any, rest: Any, seen: Any, where: str
    ) -> None:
        cid = await rec._lose_one(connector, rest, rec._LOST[0], lost_as=None)  # noqa: SLF001
        connector._userrefs[cid] = _OLD_RANGE_USERREF  # noqa: SLF001
        txid = rest.register_on_exchange(_OLD_RANGE_USERREF, where)

        await connector._resolve_unknown_orders()  # noqa: SLF001

        assert connector.fetch_order(cid).exchange_order_id == txid
        assert seen.of(base.FAILURE) == []
        assert KrakenUserrefLinkCodec().decode(connector._userrefs[cid]) is None  # noqa: SLF001


_NEGATIVE_OPEN_REPLY = {
    "error": [],
    "result": {
        "open": {"OLIVE-1": {**base._order_info(status="open"), "userref": _LIVE_NEGATIVE_USERREF}}  # noqa: SLF001
    },
}
_NEGATIVE_CLOSED_REPLY = {
    "error": [],
    "result": {
        "closed": {
            "OLIVE-2": {
                **base._order_info(status="closed", vol_exec="0.5"),  # noqa: SLF001
                "userref": _LIVE_NEGATIVE_USERREF,
            }
        },
        "count": 1,
    },
}


class TestNegativeUserrefPayloadRoundTrip:
    async def test_open_orders_payload_keeps_negative_userref(self) -> None:
        gw, _ = wire._kraken(_NEGATIVE_OPEN_REPLY)  # noqa: SLF001

        result = await gw.get_open_orders()

        assert result["OLIVE-1"].userref == _LIVE_NEGATIVE_USERREF
        assert type(result["OLIVE-1"].userref) is int

    async def test_closed_orders_payload_keeps_negative_userref(self) -> None:
        gw, _ = wire._kraken(_NEGATIVE_CLOSED_REPLY)  # noqa: SLF001

        result = await gw.get_closed_orders()

        assert result["OLIVE-2"].userref == _LIVE_NEGATIVE_USERREF
        assert type(result["OLIVE-2"].userref) is int

    async def test_negative_userref_filter_is_sent_as_signed_int(self) -> None:
        gw, sent = wire._kraken(_NEGATIVE_OPEN_REPLY)  # noqa: SLF001

        await gw.get_open_orders(userref=_LIVE_NEGATIVE_USERREF)

        assert rec._fields(sent[0])["userref"] == ["-17"]  # noqa: SLF001

    async def test_unknown_order_with_negative_userref_is_adopted(
        self, connector: Any, rest: Any, seen: Any
    ) -> None:
        cid = await rec._lose_one(connector, rest, rec._LOST[0], lost_as=None)  # noqa: SLF001
        connector._userrefs[cid] = _LIVE_NEGATIVE_USERREF  # noqa: SLF001
        txid = rest.register_on_exchange(_LIVE_NEGATIVE_USERREF, "open")

        await connector._resolve_unknown_orders()  # noqa: SLF001

        assert connector.fetch_order(cid).exchange_order_id == txid
        assert seen.of(base.FAILURE) == []
