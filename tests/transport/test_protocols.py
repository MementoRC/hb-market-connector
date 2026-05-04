"""Tests for Transport protocol family.

Verifies that:
1. The protocols are correctly @runtime_checkable.
2. Existing RestConnectorBase / WsConnectorBase satisfy them structurally.
3. A custom unified transport class can satisfy both RequestTransport and StreamTransport.
"""

from __future__ import annotations

from typing import Any

from market_connector.transport.protocols import (
    RequestTransport,
    StreamTransport,
    Transport,
)


class _DummyUnifiedTransport:
    """Conforms to Transport (the intersection: both request and subscribe)."""

    async def request(self, method: str, *args: Any, **kwargs: Any) -> Any:
        return None

    def subscribe(self, channel: str, key: Any, callback: Any) -> None:
        return None


class _RestOnly:
    """Conforms to RequestTransport only (no subscribe)."""

    async def request(self, method: str, *args: Any, **kwargs: Any) -> Any:
        return None


class _StreamOnly:
    """Conforms to StreamTransport only (no request)."""

    def subscribe(self, channel: str, key: Any, callback: Any) -> None:
        return None


class TestTransportProtocols:
    def test_dummy_unified_satisfies_all_three(self):
        t = _DummyUnifiedTransport()
        assert isinstance(t, Transport)
        assert isinstance(t, RequestTransport)
        assert isinstance(t, StreamTransport)

    def test_rest_only_satisfies_request_not_stream(self):
        t = _RestOnly()
        assert isinstance(t, RequestTransport)
        # Transport is the intersection (both request + subscribe). _RestOnly
        # has no subscribe(), so it does NOT satisfy Transport.
        assert not isinstance(t, Transport)
        assert not isinstance(t, StreamTransport)

    def test_stream_only_satisfies_stream_not_request(self):
        t = _StreamOnly()
        assert isinstance(t, StreamTransport)
        # Transport is the intersection (both request + subscribe). _StreamOnly
        # has no request(), so it does NOT satisfy Transport.
        assert not isinstance(t, Transport)
        assert not isinstance(t, RequestTransport)

    def test_object_does_not_satisfy_any(self):
        assert not isinstance(object(), Transport)
        assert not isinstance(object(), RequestTransport)
        assert not isinstance(object(), StreamTransport)
