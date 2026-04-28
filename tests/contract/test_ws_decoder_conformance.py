"""Meta-tests for WsShapeDecoderConformance suite (Task 11).

Positive case: known-good decoder + fixture frames → suite.run() passes.
Negative case: mismatched expected NormalizedWsMessage → suite.run() raises AssertionError.
"""

from __future__ import annotations

import pytest

from market_connector.testing.contract import WsShapeDecoderConformance
from market_connector.testing.spec_fixtures import (
    KNOWN_ARRAY_DECODER_SPEC,
    KNOWN_JSON_DECODER_SPEC,
)
from market_connector.ws_models.decoder import NormalizedWsMessage, WsMessageKind


def test_json_decoder_conformance_passes() -> None:
    """WsShapeDecoderConformance.run() passes for a correct JSON frame pair."""
    raw_frame = {
        "channel": "ticker",
        "product_id": "BTC-USD",
        "data": {"price": "50000"},
    }
    expected = NormalizedWsMessage(
        kind=WsMessageKind.DATA,
        channel="ticker",
        pair="BTC-USD",
        payload={"price": "50000"},
        error=None,
    )
    WsShapeDecoderConformance(
        decoder=KNOWN_JSON_DECODER_SPEC,
        fixture_frames=[(raw_frame, expected)],
    ).run()


def test_json_decoder_conformance_fails_on_wrong_expected() -> None:
    """WsShapeDecoderConformance.run() raises AssertionError when expected is wrong."""
    raw_frame = {
        "channel": "ticker",
        "product_id": "BTC-USD",
        "data": {"price": "50000"},
    }
    wrong_expected = NormalizedWsMessage(
        kind=WsMessageKind.HEARTBEAT,  # wrong kind
        channel="ticker",
        pair="BTC-USD",
        payload={"price": "50000"},
        error=None,
    )
    with pytest.raises(AssertionError):
        WsShapeDecoderConformance(
            decoder=KNOWN_JSON_DECODER_SPEC,
            fixture_frames=[(raw_frame, wrong_expected)],
        ).run()


def test_array_decoder_conformance_passes() -> None:
    """WsShapeDecoderConformance.run() passes for a correct array frame pair."""
    raw_frame = [{"bid": "49000", "ask": "50000"}, 12345, "XBT/USD", "ticker"]
    expected = NormalizedWsMessage(
        kind=WsMessageKind.DATA,
        channel="ticker",
        pair="XBT/USD",
        payload={"bid": "49000", "ask": "50000"},
        error=None,
    )
    WsShapeDecoderConformance(
        decoder=KNOWN_ARRAY_DECODER_SPEC,
        fixture_frames=[(raw_frame, expected)],
    ).run()
