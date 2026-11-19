"""Acceptance tests for issue #240 (criteria 3 and 5): Kraken userref codec + UNKNOWN bracket.

Contract pinned here (review finding 1, option A: NEGATIVE int32 userrefs only):

* ``market_connector.exchanges.kraken.linked_order_codec.KrakenUserrefLinkCodec`` satisfies
  strategy-framework's structural ``LinkedOrderIdCodec`` (``encode(group_id, leg_index)``,
  ``decode(tag)``), with class constants ``LEG_BITS = 4``, ``LEG_INDEX_MAX = 15`` and
  ``GROUP_ID_MAX = 2**27 - 1`` (leg index in the low bits, group id in the high bits).
* ``encode(group, leg) == -(1 + ((group << LEG_BITS) | leg))``: always a negative int32 in
  ``-(2**31)..-1`` (``encode(1, 0) == -17``). It raises ``ValueError``/``TypeError`` outside
  the range (never wraps or clamps).
* ``decode`` inverts ``encode`` for negative int32 values and returns ``None`` for EVERY
  userref ``>= 0`` (everything the core's counter can produce, incl. 2**30..2**31-2, and 0),
  for out-of-int32 values and for non-int input (foreign ids).
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

INT32_MIN = -(2**31)
INT32_MAX = 2**31 - 1
LEG_BITS = 4
LEG_INDEX_MAX = 15
GROUP_ID_MAX = 2**27 - 1


def _expected(group_id: int, leg: int) -> int:
    return -(1 + ((group_id << LEG_BITS) | leg))


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

    def test_pinned_layout_constants(self) -> None:
        assert KrakenUserrefLinkCodec.LEG_BITS == LEG_BITS
        assert KrakenUserrefLinkCodec.LEG_INDEX_MAX == LEG_INDEX_MAX
        assert KrakenUserrefLinkCodec.GROUP_ID_MAX == GROUP_ID_MAX

    def test_leg_index_max_covers_a_bracket(self) -> None:
        assert KrakenUserrefLinkCodec.LEG_INDEX_MAX >= 2

    def test_layout_fills_exactly_the_negative_int32_half(self) -> None:
        assert (GROUP_ID_MAX + 1) << LEG_BITS == 2**31

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
    def test_always_fits_int32_and_is_negative(
        self, codec: KrakenUserrefLinkCodec, group_id: int, leg: int
    ) -> None:
        tag = codec.encode(group_id, leg)
        assert type(tag) is int
        assert INT32_MIN <= tag <= -1
        assert tag == _expected(group_id, leg)

    def test_known_value(self, codec: KrakenUserrefLinkCodec) -> None:
        assert codec.encode(1, 0) == -17
        assert codec.decode(-17) == (1, 0)

    def test_extremes_hit_exactly_the_int32_negative_bounds(
        self, codec: KrakenUserrefLinkCodec
    ) -> None:
        assert codec.encode(0, 0) == -1
        assert codec.encode(GROUP_ID_MAX, LEG_INDEX_MAX) == INT32_MIN
        assert codec.decode(-1) == (0, 0)
        assert codec.decode(INT32_MIN) == (GROUP_ID_MAX, LEG_INDEX_MAX)

    def test_sweep(self, codec: KrakenUserrefLinkCodec) -> None:
        gmax = KrakenUserrefLinkCodec.GROUP_ID_MAX
        for group_id in range(0, gmax + 1, max(1, gmax // 997)):
            for leg in range(KrakenUserrefLinkCodec.LEG_INDEX_MAX + 1):
                tag = codec.encode(group_id, leg)
                assert INT32_MIN <= tag <= -1
                assert codec.decode(tag) == (group_id, leg)

    def test_encoding_is_injective(self, codec: KrakenUserrefLinkCodec) -> None:
        tags = {codec.encode(g, leg) for g in _group_ids() for leg in range(1 + _legs()[-1])}
        assert len(tags) == len(_group_ids()) * (1 + _legs()[-1])

    def test_legs_of_one_group_share_the_high_bits(self, codec: KrakenUserrefLinkCodec) -> None:
        tags = [codec.encode(7, leg) for leg in range(3)]
        # Undo the negation to recover (group << LEG_BITS) | leg.
        raw = [-t - 1 for t in tags]
        assert len({r >> LEG_BITS for r in raw}) == 1
        assert [r & LEG_INDEX_MAX for r in raw] == [0, 1, 2]


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
    @pytest.mark.parametrize(
        "tag", [0, 1, 2**20, 2**29, 2**30 - 1, 2**30, 2**30 + 1, 2**31 - 2, 2**31 - 1]
    )
    def test_every_non_negative_userref_is_foreign(
        self, codec: KrakenUserrefLinkCodec, tag: int
    ) -> None:
        assert codec.decode(tag) is None

    def test_sampled_non_negative_range_incl_bit_30_band(
        self, codec: KrakenUserrefLinkCodec
    ) -> None:
        step = 2**31 // 4099
        sample = set(range(0, INT32_MAX + 1, step))
        sample |= set(range(2**30, 2**30 + 1000)) | set(range(INT32_MAX - 1000, INT32_MAX + 1))
        sample |= {0, 1, 2**30 - 1, 2**30, 2**31 - 2, 2**31 - 1}
        assert [x for x in sorted(sample) if codec.decode(x) is not None] == []

    @pytest.mark.parametrize("tag", [None, True, False, 1.5, "mcl-1-0", "12345", ""])
    def test_non_int_tags(self, codec: KrakenUserrefLinkCodec, tag: object) -> None:
        assert codec.decode(tag) is None  # type: ignore[arg-type]

    @pytest.mark.parametrize("tag", [2**31, 2**40, INT32_MIN - 1, -(2**40)])
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
