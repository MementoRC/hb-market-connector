"""Canonical event-bus-backed WS routing table (ADR 0001 Group D, issue #44).

Wraps :mod:`event_bus` (hb-event-bus) so market_connector's per-exchange WS
subscription mixins and transports route inbound frames through the
canonical EventBus instead of hand-rolled dict dispatch tables. Mirrors the
wrapping style of ``hb_compat/logging.py`` (hb-logger) and follows the
precedent set by ADR 0001 Group D Target 1 (event-bus) in hb-strategy-framework.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from event_bus import EventBus, Subscription

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["EventBus", "Subscription", "WsRoutingTable"]


def _routing_key(channel: str, pair: str | None) -> str:
    """Compose the EventBus event_type key for a ``(channel, pair)`` WS routing slot."""
    return f"{channel}:{pair}" if pair is not None else f"{channel}:*"


class WsRoutingTable:
    """``(channel, pair) -> handler`` routing table backed by :class:`event_bus.EventBus`.

    Preserves the semantics of the dict-based routing table it replaces: an
    exact ``(channel, pair)`` registration takes priority; when absent,
    frames fall back to the channel-wide ``(channel, None)`` registration.
    Re-subscribing to an already-registered ``(channel, pair)`` key replaces
    the previous handler.
    """

    __slots__ = ("_bus", "_subscriptions")

    def __init__(self, *, name: str = "ws-routing") -> None:
        self._bus = EventBus(name=name)
        self._subscriptions: dict[tuple[str, str | None], Subscription] = {}

    def __len__(self) -> int:
        return len(self._subscriptions)

    def __contains__(self, key: tuple[str, str | None]) -> bool:
        return key in self._subscriptions

    def subscribe(self, channel: str, pair: str | None, handler: Callable[[Any], None]) -> None:
        """Register *handler* for the ``(channel, pair)`` routing key."""
        self.unsubscribe(channel, pair)
        self._subscriptions[(channel, pair)] = self._bus.subscribe(
            _routing_key(channel, pair), handler
        )

    def unsubscribe(self, channel: str, pair: str | None) -> None:
        """Remove the handler registered for ``(channel, pair)``. Idempotent."""
        sub = self._subscriptions.pop((channel, pair), None)
        if sub is not None:
            self._bus.unsubscribe(sub)

    def route(self, channel: str, pair: str | None, payload: Any) -> bool:
        """Dispatch *payload* to the most specific matching handler.

        Tries the exact ``(channel, pair)`` key first, then falls back to
        the channel-wide ``(channel, None)`` key.

        Returns:
            ``True`` if a matching handler was found and published to,
            ``False`` otherwise.
        """
        if pair is not None and (channel, pair) in self._subscriptions:
            self._bus.publish(_routing_key(channel, pair), payload)
            return True
        if (channel, None) in self._subscriptions:
            self._bus.publish(_routing_key(channel, None), payload)
            return True
        return False

    def clear(self) -> None:
        """Unsubscribe all registered handlers."""
        for channel, pair in list(self._subscriptions):
            self.unsubscribe(channel, pair)
