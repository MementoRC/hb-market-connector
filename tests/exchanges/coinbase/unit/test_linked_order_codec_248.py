"""RED acceptance tests for issue #248: Coinbase client_order_id LinkedOrderIdCodec.

The Protocol is owned by hb-strategy-framework; it is mirrored locally here.
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

import pytest
from market_connector.exchanges.coinbase.linked_order_codec import CoinbaseClientOrderIdLinkCodec


@runtime_checkable
class LinkedOrderIdCodec(Protocol):
    def encode(self, group_id: int, leg_index: int) -> str | int: ...
    def decode(self, tag: str | int) -> tuple[int, int] | None: ...


GROUP_IDS = [0, 1, 12345, 10**12]
LEG_INDEXES = [0, 1, 2, 15]


@pytest.fixture
def codec() -> CoinbaseClientOrderIdLinkCodec:
    return CoinbaseClientOrderIdLinkCodec()


def test_constants() -> None:
    assert CoinbaseClientOrderIdLinkCodec.LEG_INDEX_MAX == 15


def test_satisfies_protocol(codec: CoinbaseClientOrderIdLinkCodec) -> None:
    assert isinstance(codec, LinkedOrderIdCodec)


@pytest.mark.parametrize("gid", GROUP_IDS)
@pytest.mark.parametrize("leg", LEG_INDEXES)
def test_encode_format(codec: CoinbaseClientOrderIdLinkCodec, gid: int, leg: int) -> None:
    assert codec.encode(gid, leg) == f"mcl-{gid}-{leg}"


@pytest.mark.parametrize("gid", GROUP_IDS)
@pytest.mark.parametrize("leg", LEG_INDEXES)
def test_round_trip(codec: CoinbaseClientOrderIdLinkCodec, gid: int, leg: int) -> None:
    assert codec.decode(codec.encode(gid, leg)) == (gid, leg)


@pytest.mark.parametrize(
    ("gid", "leg"),
    [
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
def test_encode_rejects_invalid(
    codec: CoinbaseClientOrderIdLinkCodec, gid: object, leg: object
) -> None:
    with pytest.raises((ValueError, TypeError)):
        codec.encode(gid, leg)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "tag",
    [
        f"coinbase-{uuid.uuid4()}",
        "hbl-1-0",
        "mcl-1",
        "mcl-1-0-2",
        "mcl-01-0",
        "mcl-1-00",
        "mcl--1-0",
        "mcl-1-16",
        "MCL-1-0",
        "mcl-1-0 ",
        "",
        5,
        None,
    ],
)
def test_foreign_client_order_id_decodes_to_none(
    codec: CoinbaseClientOrderIdLinkCodec, tag: object
) -> None:
    assert codec.decode(tag) is None  # type: ignore[arg-type]
