"""Helpers to pick the right transport from a TransportAwareGateway.

OrdersMixin / AccountsMixin call request_transport_of(gateway) to get a
RequestTransport. SubscriptionsMixin / MarketDataMixin call
stream_transport_of(gateway). The helpers handle the rest_transport vs
unified_transport (and stream_transport vs unified_transport) selection
in one place so mixins stay clean.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from market_connector.protocols import TransportAwareGateway
    from market_connector.transport.protocols import RequestTransport, StreamTransport


def request_transport_of(gateway: TransportAwareGateway) -> RequestTransport:
    """Return the gateway's RequestTransport (rest or unified). Raises if absent."""
    rest = getattr(gateway, "rest_transport", None)
    if rest is not None:
        return cast("RequestTransport", rest)
    unified = getattr(gateway, "unified_transport", None)
    if unified is not None:
        return cast("RequestTransport", unified)
    raise RuntimeError(
        f"{type(gateway).__name__} has no request-capable transport "
        "(rest_transport and unified_transport are both None)"
    )


def stream_transport_of(gateway: TransportAwareGateway) -> StreamTransport:
    """Return the gateway's StreamTransport (ws or unified). Raises if absent."""
    stream = getattr(gateway, "stream_transport", None)
    if stream is not None:
        return cast("StreamTransport", stream)
    unified = getattr(gateway, "unified_transport", None)
    if unified is not None:
        return cast("StreamTransport", unified)
    raise RuntimeError(
        f"{type(gateway).__name__} has no stream-capable transport "
        "(stream_transport and unified_transport are both None)"
    )
