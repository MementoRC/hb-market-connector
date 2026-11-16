"""RED acceptance tests for issue #248: Kraken userref LinkedOrderIdCodec.

The Protocol is owned by hb-strategy-framework; it is mirrored locally here.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import pytest
from market_connector.exchanges.kraken.linked_order_codec import KrakenUserrefLinkCodec


@runtime_checkable
class LinkedOrderIdCodec(Protocol):
    def encode(self, group_id: int, leg_index: int) -> str | int: ...
    def decode(self, tag: str | int) -> tuple[int, int] | None: ...


INT32_MIN = -(2**31)
INT32_MAX = 2**31 - 1
GROUP_IDS = [0, 1, 12345, KrakenUserrefLinkCodec.GROUP_ID_MAX]
LEG_INDEXES = [0, 1, 2, 15]


@pytest.fixture
def codec() -> KrakenUserrefLinkCodec:
    return KrakenUserrefLinkCodec()


def test_constants() -> None:
    assert KrakenUserrefLinkCodec.GROUP_ID_MAX == 8_388_607
    assert KrakenUserrefLinkCodec.LEG_INDEX_MAX == 15


def test_satisfies_protocol(codec: KrakenUserrefLinkCodec) -> None:
    assert isinstance(codec, LinkedOrderIdCodec)


@pytest.mark.parametrize("gid", GROUP_IDS)
@pytest.mark.parametrize("leg", LEG_INDEXES)
def test_round_trip(codec: KrakenUserrefLinkCodec, gid: int, leg: int) -> None:
    assert codec.decode(codec.encode(gid, leg)) == (gid, leg)


@pytest.mark.parametrize("gid", GROUP_IDS)
@pytest.mark.parametrize("leg", LEG_INDEXES)
def test_encoded_is_negative_int32(codec: KrakenUserrefLinkCodec, gid: int, leg: int) -> None:
    tag = codec.encode(gid, leg)
    assert type(tag) is int
    assert INT32_MIN <= tag <= -1


@pytest.mark.parametrize(("gid", "leg"), [(0, 0), (1, 0), (7, 1), (12345, 3), (8_388_607, 15)])
def test_bit_layout(codec: KrakenUserrefLinkCodec, gid: int, leg: int) -> None:
    tag = codec.encode(gid, leg)
    assert ~tag == (0xA << 27) | (gid << 4) | leg


def test_round_trip_sweep(codec: KrakenUserrefLinkCodec) -> None:
    gmax = KrakenUserrefLinkCodec.GROUP_ID_MAX
    for gid in range(0, gmax + 1, 104_729):
        for leg in range(16):
            tag = codec.encode(gid, leg)
            assert INT32_MIN <= tag < 0
            assert codec.decode(tag) == (gid, leg)


@pytest.mark.parametrize(
    ("gid", "leg"),
    [
        (KrakenUserrefLinkCodec.GROUP_ID_MAX + 1, 0),
        (-1, 0),
        (0, 16),
        (0, -1),
        (True, 0),
        (0, True),
        (1.0, 0),
        (0, 1.0),
        ("1", 0),
        (0, "1"),
    ],
)
def test_encode_rejects_invalid(codec: KrakenUserrefLinkCodec, gid: object, leg: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        codec.encode(gid, leg)  # type: ignore[arg-type]


@pytest.mark.parametrize("userref", [1, 42, INT32_MAX, 1000, 65535, 123_456_789, 2**30])
def test_foreign_userref_decodes_to_none_positive(
    codec: KrakenUserrefLinkCodec, userref: int
) -> None:
    """foreign userref decodes to None: connector counter range (positive) is never ours."""
    assert codec.decode(userref) is None


def test_foreign_userref_decodes_to_none_counter_sample(codec: KrakenUserrefLinkCodec) -> None:
    """foreign userref decodes to None: sample of the per-order counter range."""
    for userref in range(1, 5000, 7):
        assert codec.decode(userref) is None


@pytest.mark.parametrize(
    "userref",
    [
        0,
        -1,
        ~((0x5 << 27) | (7 << 4) | 1),
        INT32_MIN - 1,
        INT32_MAX + 1,
        True,
        False,
        "mcl-1-0",
        None,
        -5.0,
    ],
)
def test_foreign_userref_decodes_to_none(codec: KrakenUserrefLinkCodec, userref: object) -> None:
    """foreign userref decodes to None (SF orphan-cancel safety)."""
    assert codec.decode(userref) is None  # type: ignore[arg-type]
