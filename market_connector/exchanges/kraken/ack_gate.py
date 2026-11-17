"""Ack gate for Kraken private subscribes: pairs ``subscriptionStatus`` acks with their reqid."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from market_connector.exceptions import SubscriptionAckTimeoutError, SubscriptionRejectedError
from market_connector.ws_models.decoder import NormalizedWsMessage, WsMessageKind

if TYPE_CHECKING:
    from collections.abc import Callable


class AckGate:
    """Hands out monotonic reqids and resolves per-reqid futures from SUBSCRIBE_ACK frames."""

    def __init__(self) -> None:
        self._next_reqid = 0
        self._pending: dict[int, asyncio.Future[None]] = {}

    def expect(self) -> tuple[int, asyncio.Future[None]]:
        """Allocate the next reqid and the future its ack will resolve."""
        self._next_reqid += 1
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._pending[self._next_reqid] = future
        return self._next_reqid, future

    def discard(self, reqid: int) -> None:
        self._pending.pop(reqid, None)

    def dispatch(self, msg: NormalizedWsMessage) -> None:
        """Resolve the future matching *msg*'s reqid; unknown or missing reqids are ignored."""
        payload = msg.payload
        if msg.kind is not WsMessageKind.SUBSCRIBE_ACK or not isinstance(payload, dict):
            return
        reqid = payload.get("reqid")
        future = self._pending.get(reqid) if isinstance(reqid, int) else None
        if future is None or future.done():
            return
        status = payload.get("status")
        if status == "subscribed":
            future.set_result(None)
        elif status == "error":
            future.set_exception(SubscriptionRejectedError(str(payload.get("errorMessage", ""))))

    async def consume(self, queue: asyncio.Queue[NormalizedWsMessage]) -> None:
        """Drain *queue* forever, dispatching each management message."""
        while True:
            self.dispatch(await queue.get())

    async def wait(self, reqid: int, future: asyncio.Future[None], timeout: float) -> None:
        """Wait for the ack of *reqid*; raise on rejection or when *timeout* elapses."""
        try:
            await asyncio.wait_for(future, timeout)
        except TimeoutError:
            raise SubscriptionAckTimeoutError(
                f"no subscribe ack for reqid {reqid} within {timeout}s"
            ) from None
        finally:
            self.discard(reqid)


async def gated_subscribe(
    ws: Any,
    gate: AckGate,
    timeout_getter: Callable[[], float],
    channel: str,
    handler: Callable[[Any], None],
    message: dict[str, Any],
) -> None:
    """Register the route, send *message* with a fresh reqid and wait for its ack.

    The route is removed again on any failure (send error, rejection, timeout, cancel).
    """
    reqid, future = gate.expect()
    ws.subscribe(channel, None, handler)
    try:
        await ws.send({**message, "reqid": reqid})
        await gate.wait(reqid, future, timeout_getter())
    except BaseException:
        gate.discard(reqid)
        ws.unsubscribe(channel, None)
        raise
