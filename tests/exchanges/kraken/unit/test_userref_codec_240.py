"""RED acceptance tests for issue #240 (criteria 3 and 5): Kraken userref codec + UNKNOWN bracket.

Contract pinned here (none of it exists yet):

* ``market_connector.exchanges.kraken.linked_order_codec.KrakenUserrefLinkCodec`` satisfies
  strategy-framework's structural ``LinkedOrderIdCodec`` (``encode(group_id, leg_index)``,
  ``decode(tag)``), with class constants ``GROUP_ID_MAX`` and ``LEG_INDEX_MAX`` (leg index in
  the low bits, group id in the high bits).
* ``encode`` returns an ``int`` in ``1..2**31-1`` (Kraken userref is int32, 0 is not a tag);
  ``decode(encode(g, leg)) == (g, leg)`` over the whole range; ``encode`` raises
  ``ValueError``/``TypeError`` outside the range (never wraps or clamps).
* Foreign tags decode to ``None``: ``str`` / ``None`` / ``bool`` / non-positive ints, and any
  userref the connector core assigns to an ordinary (non-linked) order.
(Criterion 5, UNKNOWN bracket parent resolution, lives in ``test_unknown_parent_240.py``.)
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import pytest

from market_connector.exchanges.kraken.linked_order_codec import KrakenUserrefLinkCodec
from tests.exchanges.kraken.unit import test_kraken_connector_core_211 as base
from tests.exchanges.kraken.unit import test_userref_recovery_214 as rec

# Fixtures reused as-is (pytest discovers them by module attribute).
bare_connector, bus, connector = base.bare_connector, base.bus, base.connector
gateway, seen, rest = base.gateway, base.seen, rec.rest

INT32_MAX = 2**31 - 1


@runtime_checkable
class LinkedOrderIdCodec(Protocol):
    def encode(self, group_id: int, leg_index: int) -> str | int: ...
    def decode(self, tag: str | int) -> tuple[int, int] | None: ...


@pytest.fixture
def codec() -> KrakenUserrefLinkCodec:
    return KrakenUserrefLinkCodec()


def _group_ids() -> list[int]:
    gmax = KrakenUserrefLinkCodec.GROUP_ID_MAX
    return sorted({0, 1, 2, 12345, gmax // 2, gmax - 1, gmax})


def _legs() -> list[int]:
    return sorted({0, 1, 2, KrakenUserrefLinkCodec.LEG_INDEX_MAX})


class TestConstants:
    def test_satisfies_protocol(self, codec: KrakenUserrefLinkCodec) -> None:
        assert isinstance(codec, LinkedOrderIdCodec)

    def test_leg_index_max_covers_a_bracket(self) -> None:
        assert KrakenUserrefLinkCodec.LEG_INDEX_MAX >= 2

    def test_layout_fits_int32(self) -> None:
        bits = (KrakenUserrefLinkCodec.LEG_INDEX_MAX + 1).bit_length() - 1
        assert (KrakenUserrefLinkCodec.GROUP_ID_MAX + 1) << bits <= 2**31

    def test_max_group_id_property_for_bounded_codec(self, codec: KrakenUserrefLinkCodec) -> None:
        """SF's BoundedLinkIdCodec: the manager raises instead of allocating past this."""
        assert codec.max_group_id == KrakenUserrefLinkCodec.GROUP_ID_MAX


class TestRoundTrip:
    @pytest.mark.parametrize("group_id", _group_ids())
    @pytest.mark.parametrize("leg", _legs())
    def test_round_trip(self, codec: KrakenUserrefLinkCodec, group_id: int, leg: int) -> None:
        assert codec.decode(codec.encode(group_id, leg)) == (group_id, leg)

    @pytest.mark.parametrize("group_id", _group_ids())
    @pytest.mark.parametrize("leg", _legs())
    def test_always_fits_int32(
        self, codec: KrakenUserrefLinkCodec, group_id: int, leg: int
    ) -> None:
        tag = codec.encode(group_id, leg)
        assert type(tag) is int
        assert 1 <= tag <= INT32_MAX

    def test_sweep(self, codec: KrakenUserrefLinkCodec) -> None:
        gmax = KrakenUserrefLinkCodec.GROUP_ID_MAX
        for group_id in range(0, gmax + 1, max(1, gmax // 997)):
            for leg in range(KrakenUserrefLinkCodec.LEG_INDEX_MAX + 1):
                tag = codec.encode(group_id, leg)
                assert 1 <= tag <= INT32_MAX
                assert codec.decode(tag) == (group_id, leg)

    def test_encoding_is_injective(self, codec: KrakenUserrefLinkCodec) -> None:
        tags = {codec.encode(g, leg) for g in _group_ids() for leg in range(1 + _legs()[-1])}
        assert len(tags) == len(_group_ids()) * (1 + _legs()[-1])

    def test_legs_of_one_group_share_the_high_bits(self, codec: KrakenUserrefLinkCodec) -> None:
        bits = (KrakenUserrefLinkCodec.LEG_INDEX_MAX + 1).bit_length() - 1
        tags = [codec.encode(7, leg) for leg in range(3)]
        assert len({t >> bits for t in tags}) == 1
        assert [t & KrakenUserrefLinkCodec.LEG_INDEX_MAX for t in tags] == [0, 1, 2]


class TestEncodeRejectsInvalid:
    @pytest.mark.parametrize(
        ("group_id", "leg"),
        [
            (KrakenUserrefLinkCodec.GROUP_ID_MAX + 1, 0),
            (-1, 0),
            (0, KrakenUserrefLinkCodec.LEG_INDEX_MAX + 1),
            (0, -1),
            (True, 0),
            (0, True),
            (1.0, 0),
            (0, 1.0),
            ("1", 0),
            (0, "1"),
        ],
    )
    def test_invalid_arguments(
        self, codec: KrakenUserrefLinkCodec, group_id: object, leg: object
    ) -> None:
        with pytest.raises((ValueError, TypeError)):
            codec.encode(group_id, leg)  # type: ignore[arg-type]


class TestForeignTagsDecodeToNone:
    @pytest.mark.parametrize("tag", [0, -1, -5, -(2**31)])
    def test_non_positive_userref(self, codec: KrakenUserrefLinkCodec, tag: int) -> None:
        assert codec.decode(tag) is None

    @pytest.mark.parametrize("tag", [None, True, False, 1.5, "mcl-1-0", "12345", ""])
    def test_non_int_tags(self, codec: KrakenUserrefLinkCodec, tag: object) -> None:
        assert codec.decode(tag) is None  # type: ignore[arg-type]

    @pytest.mark.parametrize("tag", [2**31, 2**40])
    def test_out_of_int32_range(self, codec: KrakenUserrefLinkCodec, tag: int) -> None:
        assert codec.decode(tag) is None

    @pytest.mark.parametrize("counter_start", [0, 1, 2**20, 2**26, 2**27, 2**29, 2**30, 2**31 - 3])
    def test_core_assigned_userrefs_are_foreign(
        self, bare_connector: Any, codec: KrakenUserrefLinkCodec, counter_start: int
    ) -> None:
        """An ordinary order's per-order counter userref must never read as a linked leg."""
        bare_connector._userref_counter = counter_start  # noqa: SLF001
        for i in range(4):
            bare_connector._assign_userref(f"cid-{counter_start}-{i}")  # noqa: SLF001
        for userref in bare_connector._userrefs.values():  # noqa: SLF001
            assert codec.decode(userref) is None, userref
