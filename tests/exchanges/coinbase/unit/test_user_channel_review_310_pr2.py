"""Review-follow-up regression tests for #310 PR 2 (Coinbase ``user`` channel).

Kept apart from the red acceptance files (``*_310_pr2.py``) on purpose: those stay untouched.
Reuses their fake socket rig.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from websockets.exceptions import ConnectionClosed

from market_connector.exchanges.coinbase.schemas.ws import UserEvent
from market_connector.exchanges.coinbase.specs import COINBASE_WS_SHAPE_DECODER_SPEC
from market_connector.exchanges.coinbase.user_channel import (
    UserChannelSession,
    parse_user_message,
)
from market_connector.transport.ws_base import WsConnectorBase
from market_connector.ws_models.decoder import JsonEnvelopeDecoder, WsMessageKind
from tests.exchanges.coinbase.unit.test_user_channel_ws_310_pr2 import (  # noqa: F401
    Rig,
    Sink,
    ec_key,
    order,
    rig,
    subscribe,
    until,
    user_msg,
)

_USER_LOGGER = "market_connector.exchanges.coinbase.user_channel"


def _session(**kwargs: Any) -> UserChannelSession:
    return UserChannelSession(
        MagicMock(),
        JsonEnvelopeDecoder(**COINBASE_WS_SHAPE_DECODER_SPEC),
        kwargs.pop("callback", lambda _e: None),
        **kwargs,
    )


def _event(type_: str, n_orders: int = 1) -> dict[str, Any]:
    return {"type": type_, "orders": [order(f"o-{i}") for i in range(n_orders)]}


# -- thread: gateway shutdown closes user sockets --------------------------------------


async def test_gateway_stop_disconnects_open_user_socket(rig: Rig) -> None:  # noqa: F811
    ctx = await subscribe(rig, Sink())
    await ctx.__aenter__()
    ws = await rig.subscribed_socket()

    await rig.gateway.stop()

    assert ws.closed is True
    assert rig.gateway._user_ws == set()


# -- thread: order fields kept ---------------------------------------------------------


def test_documented_order_fields_survive_validation_verbatim() -> None:
    parsed = UserEvent.model_validate(
        {
            "type": "update",
            "orders": [order(number_of_fills="3", limit_price="1.50", stop_price="0")],
        }
    )

    got = parsed.orders[0]
    assert got.number_of_fills == "3"
    assert got.filled_value == "250.00"
    assert got.limit_price == "1.50"
    assert got.stop_price == "0"


# -- thread: error frames and subscription acks ------------------------------------------


def test_error_frame_is_logged_at_warning_and_does_not_raise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = _session()

    with caplog.at_level(logging.WARNING):
        msg = session.decode({"type": "error", "message": "authentication failure"})

    assert msg.kind == WsMessageKind.UNKNOWN
    assert any(
        r.levelno == logging.WARNING and "authentication failure" in r.getMessage()
        for r in caplog.records
    )


def test_subscriptions_ack_is_routed_as_data_not_to_the_management_queue() -> None:
    msg = _session().decode(
        {"channel": "subscriptions", "sequence_num": 0, "events": [{"subscriptions": {}}]}
    )

    assert msg.kind == WsMessageKind.DATA
    assert msg.channel == "subscriptions"


# -- thread: callback isolation ----------------------------------------------------------


def test_raising_event_callback_does_not_drop_remaining_events() -> None:
    seen: list[str] = []

    def callback(event: UserEvent) -> None:
        seen.append(event.orders[0].order_id)
        if len(seen) == 1:
            raise RuntimeError("boom")

    session = _session(callback=callback)

    session.handle([_event("update"), _event("update")])

    assert len(seen) == 2


def test_raising_snapshot_complete_callback_does_not_stop_the_frame() -> None:
    seen: list[UserEvent] = []
    session = _session(callback=seen.append, on_snapshot_complete=MagicMock(side_effect=ValueError))

    session.handle([_event("snapshot", 1), _event("update")])

    assert len(seen) == 2


def test_raising_on_gap_still_decodes_and_tracks_sequence() -> None:
    on_gap = MagicMock(side_effect=RuntimeError("boom"))
    session = _session(on_gap=on_gap)

    first = session.decode({"channel": "user", "sequence_num": 1, "events": []})
    gapped = session.decode({"channel": "user", "sequence_num": 5, "events": []})
    after = session.decode({"channel": "user", "sequence_num": 6, "events": []})

    assert first.channel == gapped.channel == after.channel == "user"
    on_gap.assert_called_once_with(2, 5)


async def test_raising_callbacks_do_not_break_the_live_stream(rig: Rig) -> None:  # noqa: F811
    seen: list[Any] = []

    def callback(event: Any) -> None:
        seen.append(event)
        raise RuntimeError("boom")

    def bad_gap(expected: int, received: int) -> None:
        raise RuntimeError("gap boom")

    ctx = await rig.gateway.subscribe_user_orders(callback, on_gap=bad_gap)
    async with ctx:
        ws = await rig.subscribed_socket()
        ws.push(user_msg(1))
        ws.push(user_msg(7))
        ws.push(user_msg(8))
        await until(lambda: len(seen) == 3)


# -- thread: exactly-50 snapshot fallback ------------------------------------------------


def test_first_update_after_a_full_batch_snapshot_completes_it_before_delivery() -> None:
    order_of_calls: list[str] = []
    session = _session(
        callback=lambda e: order_of_calls.append(e.type),
        on_snapshot_complete=lambda: order_of_calls.append("complete"),
    )

    session.handle([_event("snapshot", 50)])
    assert order_of_calls == ["snapshot"]

    session.handle([_event("update")])
    session.handle([_event("update")])

    assert order_of_calls == ["snapshot", "complete", "update", "update"]


def test_snapshot_fallback_is_per_connection() -> None:
    completions: list[int] = []
    session = _session(on_snapshot_complete=lambda: completions.append(1))

    session.handle([_event("snapshot", 50)])
    session.handle([_event("update")])
    session._expected_seq = None
    session._snapshot_done = False
    session._snapshot_seen = False
    session.handle([_event("update")])  # update with no snapshot on this connection

    assert completions == [1]


# -- thread: cleanup and connect failure -------------------------------------------------


async def test_unsubscribe_on_closed_socket_is_suppressed_and_disconnect_runs(
    rig: Rig,  # noqa: F811
) -> None:
    async with await subscribe(rig, Sink()):
        ws = await rig.subscribed_socket()

        async def closed_send(_data: str) -> None:
            raise ConnectionClosed(None, None)

        ws.send = closed_send  # type: ignore[method-assign]

    assert ws.closed is True
    assert rig.gateway._user_ws == set()


@pytest.mark.parametrize("error", [RuntimeError("dial failed"), asyncio.CancelledError()])
async def test_failed_or_cancelled_entry_disconnects_before_reraising(
    rig: Rig,  # noqa: F811
    error: BaseException,
) -> None:
    ctx = await subscribe(rig, Sink())
    with (
        patch.object(WsConnectorBase, "connect", AsyncMock(side_effect=error)),
        patch.object(WsConnectorBase, "disconnect", AsyncMock()) as disconnect,
        pytest.raises(type(error)),
    ):
        await ctx.__aenter__()

    disconnect.assert_awaited_once()
    assert rig.gateway._user_ws == set()


# -- thread: timestamp precision ---------------------------------------------------------


def test_nine_digit_fraction_is_truncated_to_six_microsecond_digits() -> None:
    parsed = parse_user_message(
        {
            "channel": "user",
            "timestamp": "2026-04-24T12:00:07.123456789Z",
            "sequence_num": 1,
            "events": [],
        }
    )

    assert parsed.timestamp.second == 7
    assert parsed.timestamp.microsecond == 123456
