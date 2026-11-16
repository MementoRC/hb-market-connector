"""RED acceptance tests for issue #248: Kraken cl_ord_id LinkedOrderIdCodec.

The Protocol is owned by hb-strategy-framework; it is mirrored locally here.
Kraken AddOrder `cl_ord_id`: free-format ASCII, up to 18 chars, unique among
open orders, mutually exclusive with userref.
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

import pytest

from market_connector.exchanges.kraken.linked_order_codec import KrakenClOrdIdLinkCodec


@runtime_checkable
class LinkedOrderIdCodec(Protocol):
    def encode(self, group_id: int, leg_index: int) -> str | int: ...
    def decode(self, tag: str | int) -> tuple[int, int] | None: ...


GROUP_ID_MAX = 99_999_999_999
GROUP_IDS = [0, 1, 12345, 10**10, GROUP_ID_MAX]
LEG_INDEXES = [0, 1, 2, 15]
CL_ORD_ID_MAX_LEN = 18


@pytest.fixture
def codec() -> KrakenClOrdIdLinkCodec:
    return KrakenClOrdIdLinkCodec()


def test_constants() -> None:
    assert KrakenClOrdIdLinkCodec.GROUP_ID_MAX == GROUP_ID_MAX
    assert KrakenClOrdIdLinkCodec.LEG_INDEX_MAX == 15


def test_satisfies_protocol(codec: KrakenClOrdIdLinkCodec) -> None:
    assert isinstance(codec, LinkedOrderIdCodec)


@pytest.mark.parametrize("gid", GROUP_IDS)
@pytest.mark.parametrize("leg", LEG_INDEXES)
def test_encode_format(codec: KrakenClOrdIdLinkCodec, gid: int, leg: int) -> None:
    assert codec.encode(gid, leg) == f"mcl-{gid}-{leg}"


@pytest.mark.parametrize("gid", GROUP_IDS)
@pytest.mark.parametrize("leg", LEG_INDEXES)
def test_encoded_is_ascii_str_within_18_chars(
    codec: KrakenClOrdIdLinkCodec, gid: int, leg: int
) -> None:
    tag = codec.encode(gid, leg)
    assert isinstance(tag, str)
    assert tag.isascii()
    assert len(tag) <= CL_ORD_ID_MAX_LEN


def test_max_encoded_length_is_exactly_18(codec: KrakenClOrdIdLinkCodec) -> None:
    assert len(codec.encode(GROUP_ID_MAX, 15)) == CL_ORD_ID_MAX_LEN


@pytest.mark.parametrize("gid", GROUP_IDS)
@pytest.mark.parametrize("leg", LEG_INDEXES)
def test_round_trip(codec: KrakenClOrdIdLinkCodec, gid: int, leg: int) -> None:
    assert codec.decode(codec.encode(gid, leg)) == (gid, leg)


def test_round_trip_sweep(codec: KrakenClOrdIdLinkCodec) -> None:
    for gid in range(0, GROUP_ID_MAX + 1, 999_999_937):
        for leg in range(16):
            tag = codec.encode(gid, leg)
            assert len(tag) <= CL_ORD_ID_MAX_LEN
            assert codec.decode(tag) == (gid, leg)


@pytest.mark.parametrize(
    ("gid", "leg"),
    [
        (GROUP_ID_MAX + 1, 0),
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
def test_encode_rejects_invalid(codec: KrakenClOrdIdLinkCodec, gid: object, leg: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        codec.encode(gid, leg)  # type: ignore[arg-type]


@pytest.mark.parametrize("userref", [1, 42, -5, 2**31 - 1])
def test_legacy_int_userref_decodes_to_none(codec: KrakenClOrdIdLinkCodec, userref: int) -> None:
    assert codec.decode(userref) is None


@pytest.mark.parametrize(
    "tag",
    [
        "mcl-100000000000-0",
        "mcl-1",
        "mcl-1-0-2",
        "mcl-01-0",
        "mcl-1-00",
        "mcl--1-0",
        "mcl-1-16",
        "MCL-1-0",
        "mcl-1-0 ",
        "",
    ],
)
def test_malformed_tag_decodes_to_none(codec: KrakenClOrdIdLinkCodec, tag: str) -> None:
    assert codec.decode(tag) is None


def test_foreign_cl_ord_id_decodes_to_none(codec: KrakenClOrdIdLinkCodec) -> None:
    """foreign cl_ord_id decodes to None: strategy-framework orphan-cancel safety."""
    for tag in (str(uuid.uuid4()), "hbl-1-0", "hbot-buy-abc"):
        assert codec.decode(tag) is None


@pytest.mark.parametrize("tag", [None, True])
def test_non_str_non_int_decodes_to_none(codec: KrakenClOrdIdLinkCodec, tag: object) -> None:
    assert codec.decode(tag) is None  # type: ignore[arg-type]
