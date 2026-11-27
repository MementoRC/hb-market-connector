"""Coinbase ``user`` channel: envelope parser and per-connection session (#310 PR 2).

``UserChannelSession`` plugs into the shared :class:`WsConnectorBase` as its ``WsAuthModel``
(``on_connected`` sends the ``user`` + ``heartbeats`` subscribes with fresh JWTs on every
(re)connect) and as its decoder (it observes every frame, heartbeats included, so the
per-connection ``sequence_num`` can be checked before the frame is routed).
"""

from __future__ import annotations

import dataclasses
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from market_connector.exchanges.coinbase.schemas.ws import UserEvent
from market_connector.hb_compat.logging import get_logger
from market_connector.ws_models.decoder import WsMessageKind

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from market_connector.exchanges.coinbase.ws_auth import CoinbaseJwtWsAuth
    from market_connector.ws_models.auth_models import SendCallable
    from market_connector.ws_models.decoder import NormalizedWsMessage, WsShapeDecoder

USER_CHANNEL = "user"
HEARTBEATS_CHANNEL = "heartbeats"
SUBSCRIPTIONS_CHANNEL = "subscriptions"
SNAPSHOT_BATCH_SIZE = 50

logger = get_logger(__name__)

_FRACTION = re.compile(r"(\.\d{6})\d+")


class UserChannelMessage(BaseModel):
    """One ``user`` channel frame."""

    model_config = ConfigDict(frozen=True)

    channel: str
    timestamp: datetime
    sequence_num: int
    events: list[UserEvent]


def _parse_timestamp(value: str) -> datetime:
    # Coinbase sends nanosecond precision; datetime keeps microseconds.
    return datetime.fromisoformat(_FRACTION.sub(r"\1", value).replace("Z", "+00:00"))


def parse_user_message(msg: dict[str, Any]) -> UserChannelMessage:
    """Parse a raw ``user`` channel frame; numeric order fields stay strings."""
    return UserChannelMessage(
        channel=msg["channel"],
        timestamp=_parse_timestamp(msg["timestamp"]),
        sequence_num=msg["sequence_num"],
        events=[UserEvent.model_validate(e) for e in msg.get("events", [])],
    )


class UserChannelSession:
    """Subscribe frames, sequence gap tracking and event dispatch for one user socket."""

    def __init__(
        self,
        jwt_auth: CoinbaseJwtWsAuth,
        decoder: WsShapeDecoder,
        callback: Callable[[UserEvent], None],
        *,
        product_ids: Sequence[str] | None = None,
        on_snapshot_complete: Callable[[], None] | None = None,
        on_gap: Callable[[int, int], None] | None = None,
    ) -> None:
        self._jwt_auth = jwt_auth
        self._decoder = decoder
        self._callback = callback
        self._product_ids = list(product_ids) if product_ids is not None else None
        self._on_snapshot_complete = on_snapshot_complete
        self._on_gap = on_gap
        self._expected_seq: int | None = None
        self._snapshot_done = False
        self._snapshot_seen = False

    def _frame(self, type_: str, channel: str) -> dict[str, Any]:
        frame: dict[str, Any] = {"type": type_, "channel": channel}
        if channel == USER_CHANNEL and self._product_ids is not None:
            frame["product_ids"] = self._product_ids
        return frame

    # -- WsAuthModel -------------------------------------------------------

    async def prepare_connection(self, base_url: str) -> str:
        return base_url

    async def on_connected(self, ws_send: SendCallable) -> None:
        # Sent without waiting for any server frame: Coinbase drops sockets that do not
        # subscribe within 5s. Runs on every (re)connect, so each one gets fresh JWTs.
        # TODO(#310 live probe): whether a reconnect replays a snapshot or only updates.
        self._expected_seq = None
        self._snapshot_done = False
        self._snapshot_seen = False
        for channel in (USER_CHANNEL, HEARTBEATS_CHANNEL):
            await ws_send(
                await self._jwt_auth.transform_outgoing(self._frame("subscribe", channel))
            )

    async def transform_outgoing(self, msg: dict[str, Any]) -> dict[str, Any]:
        return await self._jwt_auth.transform_outgoing(msg)

    async def refresh(self) -> None:
        pass

    # -- WsShapeDecoder ----------------------------------------------------

    def decode(self, raw: dict | list | str) -> NormalizedWsMessage:
        if isinstance(raw, dict):
            if raw.get("type") == "error":
                logger.warning("Coinbase user socket error frame: %s", raw)
            self._track_sequence(raw.get("sequence_num"))
        msg = self._decoder.decode(raw)
        if msg.channel == SUBSCRIPTIONS_CHANNEL:
            # Route acks to a no-op handler instead of the unbounded management queue.
            return dataclasses.replace(msg, kind=WsMessageKind.DATA)
        return msg

    def _track_sequence(self, received: object) -> None:
        if not isinstance(received, int):
            return
        # TODO(#310 live probe): starting sequence_num; the first frame seeds the count.
        expected = self._expected_seq
        self._expected_seq = received + 1
        if expected is not None and received != expected and self._on_gap is not None:
            self._safe(self._on_gap, "on_gap", expected, received)

    @staticmethod
    def _safe(fn: Callable[..., object], name: str, *args: Any) -> None:
        """Run a user callback; an exception is logged and never breaks frame processing."""
        try:
            fn(*args)
        except Exception:
            logger.exception("Coinbase user channel %s callback raised", name)

    def handle_subscriptions(self, payload: Any) -> None:
        """No-op handler for ``subscriptions`` acks (logged only)."""
        logger.debug("Coinbase user socket subscriptions ack: %r", payload)

    # -- routing -----------------------------------------------------------

    def unsubscribe_frames(self) -> list[dict[str, Any]]:
        return [self._frame("unsubscribe", c) for c in (USER_CHANNEL, HEARTBEATS_CHANNEL)]

    def handle(self, payload: Any) -> None:
        """Routing-table handler: ``payload`` is the frame's ``events`` list."""
        for raw_event in payload:
            event = UserEvent.model_validate(raw_event)
            if event.type == "update" and self._snapshot_seen and not self._snapshot_done:
                # Fallback for the unverified exactly-50 case: live updates imply the
                # snapshot is over, so complete it before delivering the first update.
                self._complete_snapshot()
            self._safe(self._callback, "event", event)
            self._check_snapshot(event)

    def _complete_snapshot(self) -> None:
        self._snapshot_done = True
        if self._on_snapshot_complete is not None:
            self._safe(self._on_snapshot_complete, "on_snapshot_complete")

    def _check_snapshot(self, event: UserEvent) -> None:
        # TODO(#310 live probe): a final snapshot batch of exactly 50 orders is ambiguous;
        # a full batch alone never completes the snapshot (the first update does, see handle).
        if event.type != "snapshot" or self._snapshot_done:
            return
        self._snapshot_seen = True
        if len(event.orders) < SNAPSHOT_BATCH_SIZE:
            self._complete_snapshot()
