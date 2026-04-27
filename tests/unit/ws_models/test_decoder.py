"""Tests for ws_models.decoder."""

from __future__ import annotations

from market_connector.ws_models.decoder import (
    JsonEnvelopeDecoder,
    NormalizedWsMessage,
    PositionalArrayDecoder,
    WsMessageKind,
    WsShapeDecoder,
)

# ---------------------------------------------------------------------------
# JsonEnvelopeDecoder
# ---------------------------------------------------------------------------


class TestJsonEnvelopeDecoderData:
    """Coinbase-style: JSON envelope with top-level channel and nested pair."""

    COINBASE_FRAME = {
        "channel": "level2",
        "events": [{"product_id": "BTC-USD", "type": "snapshot", "updates": []}],
    }

    def test_data_kind_default(self) -> None:
        """Frame not in kind_dispatch resolves to DATA."""
        decoder = JsonEnvelopeDecoder(
            channel_field="channel",
            pair_field=lambda m: m["events"][0]["product_id"],
            payload_field="events",
            kind_dispatch={},
        )
        msg = decoder.decode(self.COINBASE_FRAME)
        assert msg.kind == WsMessageKind.DATA

    def test_channel_extracted_via_string_key(self) -> None:
        decoder = JsonEnvelopeDecoder(
            channel_field="channel",
            pair_field=lambda m: m["events"][0]["product_id"],
            payload_field="events",
            kind_dispatch={},
        )
        msg = decoder.decode(self.COINBASE_FRAME)
        assert msg.channel == "level2"

    def test_pair_extracted_via_callable(self) -> None:
        decoder = JsonEnvelopeDecoder(
            channel_field="channel",
            pair_field=lambda m: m["events"][0]["product_id"],
            payload_field="events",
            kind_dispatch={},
        )
        msg = decoder.decode(self.COINBASE_FRAME)
        assert msg.pair == "BTC-USD"

    def test_payload_extracted_via_string_key(self) -> None:
        decoder = JsonEnvelopeDecoder(
            channel_field="channel",
            pair_field=lambda m: m["events"][0]["product_id"],
            payload_field="events",
            kind_dispatch={},
        )
        msg = decoder.decode(self.COINBASE_FRAME)
        assert msg.payload == self.COINBASE_FRAME["events"]

    def test_full_normalized_message(self) -> None:
        """End-to-end: Coinbase frame → NormalizedWsMessage with all fields correct."""
        decoder = JsonEnvelopeDecoder(
            channel_field="channel",
            pair_field=lambda m: m["events"][0]["product_id"],
            payload_field="events",
            kind_dispatch={},
        )
        msg = decoder.decode(self.COINBASE_FRAME)
        expected = NormalizedWsMessage(
            kind=WsMessageKind.DATA,
            channel="level2",
            pair="BTC-USD",
            payload=self.COINBASE_FRAME["events"],
            error=None,
        )
        assert msg == expected


class TestJsonEnvelopeDecoderHeartbeat:
    """Heartbeat frame resolved via kind_dispatch."""

    HEARTBEAT_FRAME = {
        "channel": "heartbeats",
        "client_id": "",
        "timestamp": "2024-01-01T00:00:00Z",
        "sequence_num": 0,
        "events": [],
    }

    def test_heartbeat_kind_via_dispatch(self) -> None:
        decoder = JsonEnvelopeDecoder(
            channel_field="channel",
            pair_field=None,
            payload_field="events",
            kind_dispatch={"heartbeats": WsMessageKind.HEARTBEAT},
        )
        msg = decoder.decode(self.HEARTBEAT_FRAME)
        assert msg.kind == WsMessageKind.HEARTBEAT

    def test_heartbeat_channel(self) -> None:
        decoder = JsonEnvelopeDecoder(
            channel_field="channel",
            pair_field=None,
            payload_field="events",
            kind_dispatch={"heartbeats": WsMessageKind.HEARTBEAT},
        )
        msg = decoder.decode(self.HEARTBEAT_FRAME)
        assert msg.channel == "heartbeats"

    def test_pair_none_when_pair_field_none(self) -> None:
        decoder = JsonEnvelopeDecoder(
            channel_field="channel",
            pair_field=None,
            payload_field="events",
            kind_dispatch={"heartbeats": WsMessageKind.HEARTBEAT},
        )
        msg = decoder.decode(self.HEARTBEAT_FRAME)
        assert msg.pair is None


class TestJsonEnvelopeDecoderCallablePairField:
    """Explicit test for callable pair_field path (Coinbase nested product_id)."""

    def test_callable_pair_field_is_invoked_with_raw(self) -> None:
        raw = {
            "channel": "ticker",
            "events": [{"product_id": "ETH-USD"}],
        }
        invoked_with: list[dict] = []

        def capture_pair(msg: dict) -> str:
            invoked_with.append(msg)
            return msg["events"][0]["product_id"]

        decoder = JsonEnvelopeDecoder(
            channel_field="channel",
            pair_field=capture_pair,
            payload_field="events",
            kind_dispatch={},
        )
        msg = decoder.decode(raw)
        assert msg.pair == "ETH-USD"
        assert invoked_with == [raw]


# ---------------------------------------------------------------------------
# PositionalArrayDecoder
# ---------------------------------------------------------------------------


class TestPositionalArrayDecoderData:
    """Kraken-style: positional array [seq_id, payload, pair, channel].

    Real Kraken v1 WS format: [channelID, payload, pair, channelName]
    So channel_index=-1 (last) and pair_index=-2 (second-to-last).
    """

    KRAKEN_FRAME: list = [330, {"a": [["50000", "1", "1"]]}, "XBT/USD", "book-10"]

    def test_channel_extracted_by_index(self) -> None:
        decoder = PositionalArrayDecoder(
            channel_index=-1,
            pair_index=-2,
            payload_index=1,
            heartbeat_predicate=None,
            subscribe_ack_predicate=None,
        )
        msg = decoder.decode(self.KRAKEN_FRAME)
        assert msg.channel == "book-10"

    def test_pair_extracted_by_index(self) -> None:
        decoder = PositionalArrayDecoder(
            channel_index=-1,
            pair_index=-2,
            payload_index=1,
            heartbeat_predicate=None,
            subscribe_ack_predicate=None,
        )
        msg = decoder.decode(self.KRAKEN_FRAME)
        assert msg.pair == "XBT/USD"

    def test_payload_extracted_by_index(self) -> None:
        decoder = PositionalArrayDecoder(
            channel_index=-1,
            pair_index=-2,
            payload_index=1,
            heartbeat_predicate=None,
            subscribe_ack_predicate=None,
        )
        msg = decoder.decode(self.KRAKEN_FRAME)
        assert msg.payload == {"a": [["50000", "1", "1"]]}

    def test_kind_is_data(self) -> None:
        decoder = PositionalArrayDecoder(
            channel_index=-1,
            pair_index=-2,
            payload_index=1,
            heartbeat_predicate=None,
            subscribe_ack_predicate=None,
        )
        msg = decoder.decode(self.KRAKEN_FRAME)
        assert msg.kind == WsMessageKind.DATA

    def test_full_normalized_message(self) -> None:
        decoder = PositionalArrayDecoder(
            channel_index=-1,
            pair_index=-2,
            payload_index=1,
            heartbeat_predicate=None,
            subscribe_ack_predicate=None,
        )
        msg = decoder.decode(self.KRAKEN_FRAME)
        expected = NormalizedWsMessage(
            kind=WsMessageKind.DATA,
            channel="book-10",
            pair="XBT/USD",
            payload={"a": [["50000", "1", "1"]]},
            error=None,
        )
        assert msg == expected


class TestPositionalArrayDecoderHeartbeat:
    """Heartbeat predicate triggers HEARTBEAT kind."""

    def test_heartbeat_predicate_triggers_kind(self) -> None:
        decoder = PositionalArrayDecoder(
            channel_index=-1,
            pair_index=-2,
            payload_index=1,
            heartbeat_predicate=lambda raw: raw[-1] == "heartbeat",
            subscribe_ack_predicate=None,
        )
        # Frame where predicate returns True: [id, payload, pair, "heartbeat"]
        frame: list = [0, {}, "XBT/USD", "heartbeat"]
        msg = decoder.decode(frame)
        assert msg.kind == WsMessageKind.HEARTBEAT

    def test_heartbeat_predicate_false_gives_data(self) -> None:
        decoder = PositionalArrayDecoder(
            channel_index=-1,
            pair_index=-2,
            payload_index=1,
            heartbeat_predicate=lambda raw: False,
            subscribe_ack_predicate=None,
        )
        frame: list = [330, {"a": []}, "XBT/USD", "book-10"]
        msg = decoder.decode(frame)
        assert msg.kind == WsMessageKind.DATA


class TestPositionalArrayDecoderSubscribeAck:
    """Subscribe-ack predicate: Kraken acks arrive as dicts (not arrays)."""

    KRAKEN_SUBSCRIBE_ACK = {
        "channelID": 42,
        "channelName": "ticker",
        "event": "subscriptionStatus",
        "pair": "XBT/USD",
        "status": "subscribed",
        "subscription": {"name": "ticker"},
    }

    def test_subscribe_ack_predicate_triggers_kind(self) -> None:
        decoder = PositionalArrayDecoder(
            channel_index=-1,
            pair_index=None,
            payload_index=0,
            heartbeat_predicate=None,
            subscribe_ack_predicate=lambda raw: (
                isinstance(raw, dict) and raw.get("event") == "subscriptionStatus"
            ),
        )
        msg = decoder.decode(self.KRAKEN_SUBSCRIBE_ACK)
        assert msg.kind == WsMessageKind.SUBSCRIBE_ACK

    def test_subscribe_ack_payload_is_raw_dict(self) -> None:
        decoder = PositionalArrayDecoder(
            channel_index=-1,
            pair_index=None,
            payload_index=0,
            heartbeat_predicate=None,
            subscribe_ack_predicate=lambda raw: (
                isinstance(raw, dict) and raw.get("event") == "subscriptionStatus"
            ),
        )
        msg = decoder.decode(self.KRAKEN_SUBSCRIBE_ACK)
        assert msg.payload == self.KRAKEN_SUBSCRIBE_ACK


# ---------------------------------------------------------------------------
# Protocol structural conformance
# ---------------------------------------------------------------------------


class TestWsShapeDecoderProtocol:
    """JsonEnvelopeDecoder and PositionalArrayDecoder satisfy WsShapeDecoder structurally."""

    def test_json_envelope_decoder_satisfies_protocol(self) -> None:
        decoder: WsShapeDecoder = JsonEnvelopeDecoder(
            channel_field="channel",
            pair_field=None,
            payload_field="data",
            kind_dispatch={},
        )
        assert hasattr(decoder, "decode")

    def test_positional_array_decoder_satisfies_protocol(self) -> None:
        decoder: WsShapeDecoder = PositionalArrayDecoder(
            channel_index=-1,
            pair_index=None,
            payload_index=1,
            heartbeat_predicate=None,
            subscribe_ack_predicate=None,
        )
        assert hasattr(decoder, "decode")
