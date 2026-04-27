"""WS message shape decoder: wire format → NormalizedWsMessage.

Stage 1 decode (framework layer): extracts routing keys (channel, pair) and
classifies the message kind.  Exchange-specific Pydantic models parse
``NormalizedWsMessage.payload`` in Stage 2.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable


class WsMessageKind(Enum):
    """Classification of a WebSocket frame."""

    DATA = "data"
    HEARTBEAT = "heartbeat"
    SUBSCRIBE_ACK = "subscribe_ack"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class NormalizedWsMessage:
    """Shape-decoded WebSocket message ready for routing."""

    kind: WsMessageKind
    channel: str | None
    pair: str | None
    payload: dict | list
    error: str | None


@runtime_checkable
class WsShapeDecoder(Protocol):
    """Structural protocol satisfied by any decoder that can decode a raw frame."""

    def decode(self, raw: dict | list | str) -> NormalizedWsMessage: ...


def _extract(field: str | Callable, raw: dict) -> str | None:
    """Apply a string key lookup or callable extractor against *raw*."""
    if callable(field):
        return field(raw)
    return raw.get(field)


@dataclass(frozen=True)
class JsonEnvelopeDecoder:
    """Decoder for JSON-object frames (Coinbase, Bybit, OKX, etc.).

    Field extractors are either a plain string (dict key) or a callable
    invoked with the raw message dict.
    """

    channel_field: str | Callable[[dict], str]
    pair_field: str | Callable[[dict], str | None] | None
    payload_field: str
    kind_dispatch: dict[str, WsMessageKind]
    error_field: str | None = None

    def decode(self, raw: dict | list | str) -> NormalizedWsMessage:  # type: ignore[override]
        assert isinstance(raw, dict), "JsonEnvelopeDecoder expects a dict frame"
        channel = _extract(self.channel_field, raw)
        pair = _extract(self.pair_field, raw) if self.pair_field is not None else None
        payload = raw.get(self.payload_field, {})
        error: str | None = None
        if self.error_field is not None:
            error = raw.get(self.error_field)

        if channel:
            kind = self.kind_dispatch.get(channel, WsMessageKind.DATA)
        else:
            kind = WsMessageKind.UNKNOWN
        if error:
            kind = WsMessageKind.ERROR

        return NormalizedWsMessage(
            kind=kind, channel=channel, pair=pair, payload=payload, error=error
        )


@dataclass(frozen=True)
class PositionalArrayDecoder:
    """Decoder for positional-array frames (Kraken, Bitfinex-legacy, etc.).

    Indices may be negative (e.g. ``channel_index=-1`` for last element).
    The subscribe-ack predicate is also invoked on dict frames because
    Kraken subscription acknowledgements arrive as JSON objects, not arrays.
    """

    channel_index: int
    pair_index: int | None
    payload_index: int
    heartbeat_predicate: Callable[[list], bool] | None
    subscribe_ack_predicate: Callable[[dict | list], bool] | None

    def decode(self, raw: dict | list | str) -> NormalizedWsMessage:  # type: ignore[override]
        # Subscribe-ack predicate is checked first; Kraken acks are dicts.
        if self.subscribe_ack_predicate is not None and self.subscribe_ack_predicate(raw):
            return NormalizedWsMessage(
                kind=WsMessageKind.SUBSCRIBE_ACK,
                channel=None,
                pair=None,
                payload=raw,  # type: ignore[arg-type]
                error=None,
            )

        assert isinstance(raw, list), "PositionalArrayDecoder expects a list frame (or ack dict)"

        if self.heartbeat_predicate is not None and self.heartbeat_predicate(raw):
            channel = raw[self.channel_index] if self.channel_index is not None else None
            pair = raw[self.pair_index] if self.pair_index is not None else None
            return NormalizedWsMessage(
                kind=WsMessageKind.HEARTBEAT,
                channel=channel,
                pair=pair,
                payload=raw[self.payload_index],
                error=None,
            )

        channel = raw[self.channel_index]
        pair = raw[self.pair_index] if self.pair_index is not None else None
        payload = raw[self.payload_index]
        return NormalizedWsMessage(
            kind=WsMessageKind.DATA, channel=channel, pair=pair, payload=payload, error=None
        )
