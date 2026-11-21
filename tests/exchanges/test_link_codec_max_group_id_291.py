"""Acceptance tests for issue #291: ``max_group_id`` on prefixed link codecs.

Plain pytest functions only. No strategy-framework import (ADR 0001); the
bounded-codec contract is mirrored locally as ``_BoundedLinkIdCodec``.

``CoinbaseClientOrderIdLinkCodec`` is unbounded (``GROUP_ID_MAX is None``), so the
boundary tests (encode at max, encode above max, round trip at max) run only on the
bounded codecs.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import pytest

from market_connector.exchanges.coinbase.linked_order_codec import CoinbaseClientOrderIdLinkCodec
from market_connector.exchanges.kraken.linked_order_codec import (
    KrakenClOrdIdLinkCodec,
    KrakenUserrefLinkCodec,
)

KRAKEN_CL_ORD_ID_MAX_GROUP_ID = 99_999_999_999
KRAKEN_CL_ORD_ID_MAX_LENGTH = 18

PREFIXED_CODECS = [KrakenClOrdIdLinkCodec, CoinbaseClientOrderIdLinkCodec]
BOUNDED_CODECS = [KrakenClOrdIdLinkCodec, KrakenUserrefLinkCodec]
ALL_CODECS = [KrakenClOrdIdLinkCodec, CoinbaseClientOrderIdLinkCodec, KrakenUserrefLinkCodec]


@runtime_checkable
class _BoundedLinkIdCodec(Protocol):
    """Local mirror of the bounded codec contract (no strategy-framework import)."""

    def encode(self, group_id: int, leg: int) -> str | int: ...

    def decode(self, tag: str | int) -> tuple[int, int] | None: ...

    @property
    def max_group_id(self) -> int | None: ...


@pytest.mark.parametrize("codec_cls", PREFIXED_CODECS)
def test_max_group_id_mirrors_group_id_max_constant(codec_cls: type) -> None:
    assert codec_cls().max_group_id == codec_cls.GROUP_ID_MAX


def test_kraken_cl_ord_id_max_group_id_is_11_digits() -> None:
    assert KrakenClOrdIdLinkCodec().max_group_id == KRAKEN_CL_ORD_ID_MAX_GROUP_ID


@pytest.mark.parametrize("codec_cls", ALL_CODECS)
def test_codec_satisfies_bounded_link_id_protocol(codec_cls: type) -> None:
    assert isinstance(codec_cls(), _BoundedLinkIdCodec)


@pytest.mark.parametrize("codec_cls", BOUNDED_CODECS)
def test_encode_accepts_max_group_id_at_max_leg(codec_cls: type) -> None:
    codec = codec_cls()
    tag = codec.encode(codec.max_group_id, codec.LEG_INDEX_MAX)
    assert tag is not None


@pytest.mark.parametrize("codec_cls", BOUNDED_CODECS)
def test_encode_rejects_group_id_above_max(codec_cls: type) -> None:
    codec = codec_cls()
    with pytest.raises(ValueError):
        codec.encode(codec.max_group_id + 1, codec.LEG_INDEX_MAX)


def test_kraken_cl_ord_id_at_max_is_ascii_within_18_chars() -> None:
    codec = KrakenClOrdIdLinkCodec()
    tag = codec.encode(codec.max_group_id, codec.LEG_INDEX_MAX)
    assert isinstance(tag, str)
    assert tag.isascii()
    assert len(tag) <= KRAKEN_CL_ORD_ID_MAX_LENGTH


@pytest.mark.parametrize("codec_cls", BOUNDED_CODECS)
def test_round_trip_at_max_group_id(codec_cls: type) -> None:
    codec = codec_cls()
    leg = codec.LEG_INDEX_MAX
    assert codec.decode(codec.encode(codec.max_group_id, leg)) == (codec.max_group_id, leg)


@pytest.mark.parametrize("codec_cls", ALL_CODECS)
def test_max_group_id_is_read_only(codec_cls: type) -> None:
    codec = codec_cls()
    with pytest.raises(AttributeError):
        codec.max_group_id = 1  # type: ignore[misc]
