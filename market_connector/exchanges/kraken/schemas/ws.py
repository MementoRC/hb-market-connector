"""Kraken WebSocket message schema models (Pydantic v2).

Kraken v1 WS messages come in two forms:

1. **Dict-frame** (control messages): Plain JSON objects with an ``"event"``
   key.  These are pre-classified by ``KrakenWsDecoder`` (Stage 1) into
   ``HEARTBEAT``, ``SUBSCRIBE_ACK``, or ``UNKNOWN`` kinds before payload
   parsing.  Models: ``Heartbeat``, ``SystemStatus``, ``SubscriptionAck``.

2. **Array-frame** (data channels): JSON arrays decoded by
   ``PositionalArrayDecoder``.  The framework normalises them into
   ``NormalizedWsMessage`` objects; Stage 3 schemas model the *payload*
   portion (``NormalizedWsMessage.payload``).

Array-frame payload conventions
--------------------------------
- Public channels: ``payload`` is the inner data item at index 1.
  - book:  ``{"as": [...], "bs": [...]}`` or ``{"a": [...], "b": [...]}``
  - trade: ``[[price, volume, time, side, type, misc], ...]``
- Private channels (ownTrades, openOrders): ``payload`` is the raw frame list
  element at index 1.  The last element is a ``{"sequence": N}`` metadata dict
  (not a string pair), which is a documented Kraken v1 WS quirk.

Design choice: dict-form control frames use Pydantic BaseModel.  Array-frame
payloads that are already plain Python dicts/lists use TypedDict or simple
dataclasses — this avoids overhead from Pydantic validation on high-frequency
data paths while keeping type annotations for IDE support.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


# ---------------------------------------------------------------------------
# Dict-frame control messages
# ---------------------------------------------------------------------------


class Heartbeat(_Base):
    """``{"event": "heartbeat"}``"""

    event: str = "heartbeat"


class SystemStatus(_Base):
    """``{"event": "systemStatus", "connectionID": ..., "status": ..., "version": ...}``"""

    event: str
    connection_id: int | None = Field(default=None, validation_alias="connectionID")
    status: str = ""
    version: str = ""


class SubscriptionDetail(_Base):
    name: str = ""
    interval: int | None = None
    depth: int | None = None
    token: str | None = None


class SubscriptionAck(_Base):
    """``{"event": "subscriptionStatus", "status": "subscribed"|"unsubscribed", ...}``"""

    event: str
    channel_id: int | None = Field(default=None, validation_alias="channelID")
    channel_name: str | None = Field(default=None, validation_alias="channelName")
    pair: str | None = None
    reqid: int | None = None
    status: str = ""
    subscription: SubscriptionDetail | None = None
    error_message: str | None = Field(default=None, validation_alias="errorMessage")


# ---------------------------------------------------------------------------
# Array-frame payload models
# ---------------------------------------------------------------------------


class BookLevel(BaseModel):
    """A single price-level entry from Kraken book snapshots / updates.

    Kraken returns book levels as ``[price_str, volume_str, timestamp_str]``
    or ``[price_str, volume_str, timestamp_str, update_type]``.
    """

    model_config = ConfigDict(frozen=True)

    price: str
    volume: str
    timestamp: str
    update_type: str | None = None

    @classmethod
    def from_list(cls, raw: list[str]) -> BookLevel:
        return cls(
            price=raw[0],
            volume=raw[1],
            timestamp=raw[2],
            update_type=raw[3] if len(raw) > 3 else None,
        )


class BookSnapshot(BaseModel):
    """Payload from a book-snapshot array frame.

    The NormalizedWsMessage.payload for a book snapshot is a dict with
    ``"as"`` (asks) and ``"bs"`` (bids) keys, each containing a list of
    ``[price, volume, timestamp]`` entries.

    Example payload::

        {
            "as": [["5541.30000", "2.50700000", "1534614248.123678"], ...],
            "bs": [["5541.20000", "1.52900000", "1534614248.765567"], ...],
        }
    """

    model_config = ConfigDict(frozen=True)

    asks: list[BookLevel] = Field(default_factory=list)
    bids: list[BookLevel] = Field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> BookSnapshot:
        """Build from the inner dict payload of a book-snapshot array frame."""
        return cls(
            asks=[BookLevel.from_list(e) for e in payload.get("as", [])],
            bids=[BookLevel.from_list(e) for e in payload.get("bs", [])],
        )


class BookUpdate(BaseModel):
    """Payload from a book-update array frame.

    Similar to BookSnapshot but uses ``"a"`` / ``"b"`` keys and entries may
    include a 4th ``update_type`` field (``"r"`` for republish).
    """

    model_config = ConfigDict(frozen=True)

    asks: list[BookLevel] = Field(default_factory=list)
    bids: list[BookLevel] = Field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> BookUpdate:
        return cls(
            asks=[BookLevel.from_list(e) for e in payload.get("a", [])],
            bids=[BookLevel.from_list(e) for e in payload.get("b", [])],
        )


class Trade(BaseModel):
    """A single trade entry from a Kraken trade-channel array frame.

    Kraken trade payload is a list of ``[price, volume, time, side, type, misc]``
    entries.  ``side`` is ``"b"`` (buy) or ``"s"`` (sell).
    """

    model_config = ConfigDict(frozen=True)

    price: str
    volume: str
    time: str
    side: str
    order_type: str
    misc: str = ""

    @classmethod
    def from_list(cls, raw: list[str]) -> Trade:
        return cls(
            price=raw[0],
            volume=raw[1],
            time=raw[2],
            side=raw[3],
            order_type=raw[4],
            misc=raw[5] if len(raw) > 5 else "",
        )


class TradeEvent(BaseModel):
    """All trades in a single trade-channel array frame."""

    model_config = ConfigDict(frozen=True)

    trades: list[Trade]
    channel: str
    pair: str

    @classmethod
    def from_frame(cls, raw: list[Any]) -> TradeEvent:
        """Build from the raw decoded array frame ``[seq, trades_list, "trade", pair]``."""
        return cls(
            trades=[Trade.from_list(t) for t in raw[1]],
            channel=raw[-2],
            pair=raw[-1],
        )


class OwnTradeDetail(BaseModel):
    """Detail record for a single own trade."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    cost: str = "0"
    fee: str = "0"
    margin: str = "0"
    ordertxid: str = ""
    ordertype: str = ""
    pair: str = ""
    postxid: str = ""
    price: str = "0"
    time: str = ""
    type: str = ""
    vol: str = "0"


class OwnTradesEvent(BaseModel):
    """Private channel ownTrades event.

    Kraken ownTrades frames have the structure::

        [
            [{"txid1": {trade_detail}}, {"txid2": {trade_detail}}, ...],
            "ownTrades",
            {"sequence": N}
        ]

    ``NormalizedWsMessage.payload`` is ``raw[1]`` (the string ``"ownTrades"``
    under the framework's PositionalArrayDecoder with pair_index=-1 and
    payload_index=1).  The actual trades list is at ``raw[0]``.

    Note: this is a known Kraken v1 WS protocol quirk documented in Stage 2.
    This model is provided for direct raw-frame parsing; use
    ``OwnTradesEvent.from_raw_frame()`` to build from the full raw list.
    """

    model_config = ConfigDict(frozen=True)

    trades: dict[str, OwnTradeDetail]
    sequence: int

    @classmethod
    def from_raw_frame(cls, raw: list[Any]) -> OwnTradesEvent:
        """Build from a raw ownTrades frame list.

        Merges all per-trade dicts in ``raw[0]`` into a single dict.
        ``raw[-1]`` is expected to be ``{"sequence": N}``.
        """
        trades_list: list[dict[str, Any]] = raw[0]
        merged: dict[str, OwnTradeDetail] = {}
        for entry in trades_list:
            for txid, detail in entry.items():
                merged[txid] = OwnTradeDetail.model_validate(detail)
        seq_dict: dict[str, Any] = raw[-1] if isinstance(raw[-1], dict) else {}
        return cls(trades=merged, sequence=seq_dict.get("sequence", 0))


class OpenOrderDetail(BaseModel):
    """Detail record for a single open order from the openOrders channel."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    status: str = ""
    vol: str = "0"
    vol_exec: str = "0"
    cost: str = "0"
    fee: str = "0"
    avg_price: str = "0"


class OpenOrdersEvent(BaseModel):
    """Private channel openOrders event.

    Analogous to OwnTradesEvent — the frame structure is::

        [
            [{"order_id1": {order_detail}}, ...],
            "openOrders",
            {"sequence": N}
        ]
    """

    model_config = ConfigDict(frozen=True)

    orders: dict[str, OpenOrderDetail]
    sequence: int

    @classmethod
    def from_raw_frame(cls, raw: list[Any]) -> OpenOrdersEvent:
        orders_list: list[dict[str, Any]] = raw[0]
        merged: dict[str, OpenOrderDetail] = {}
        for entry in orders_list:
            for order_id, detail in entry.items():
                merged[order_id] = OpenOrderDetail.model_validate(detail)
        seq_dict: dict[str, Any] = raw[-1] if isinstance(raw[-1], dict) else {}
        return cls(orders=merged, sequence=seq_dict.get("sequence", 0))
