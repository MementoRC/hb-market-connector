"""Tests for transport dispatch helpers."""

from __future__ import annotations

import pytest

from market_connector.transport.dispatch import (
    request_transport_of,
    stream_transport_of,
)


class _Aware:
    def __init__(self, rest=None, stream=None, unified=None):
        self.rest_transport = rest
        self.stream_transport = stream
        self.unified_transport = unified
        self.contract_resolver = None

    @property
    def ready(self) -> bool:
        return False

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def place_order(self, *args, **kwargs): ...

    async def cancel_order(self, *args, **kwargs): ...

    async def get_open_orders(self, *args, **kwargs): ...

    async def get_balance(self, *args, **kwargs): ...

    async def get_orderbook(self, *args, **kwargs): ...

    async def get_candles(self, *args, **kwargs): ...

    async def get_mid_price(self, *args, **kwargs): ...

    async def subscribe_orderbook(self, *args, **kwargs): ...

    async def subscribe_trades(self, *args, **kwargs): ...


class _DummyTransport:
    @property
    def is_connected(self) -> bool:
        return True

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def request(self, method, *args, **kwargs):
        return None

    def subscribe(self, channel, key, callback):
        return None


class TestRequestTransportOf:
    def test_returns_rest_when_rest_set(self):
        rest = _DummyTransport()
        g = _Aware(rest=rest)
        assert request_transport_of(g) is rest

    def test_returns_unified_when_only_unified_set(self):
        unified = _DummyTransport()
        g = _Aware(unified=unified)
        assert request_transport_of(g) is unified

    def test_prefers_rest_over_unified(self):
        rest = _DummyTransport()
        unified = _DummyTransport()
        g = _Aware(rest=rest, unified=unified)
        assert request_transport_of(g) is rest

    def test_raises_when_no_transport_set(self):
        g = _Aware()
        with pytest.raises(RuntimeError, match="no request-capable transport"):
            request_transport_of(g)


class TestStreamTransportOf:
    def test_returns_stream_when_stream_set(self):
        stream = _DummyTransport()
        g = _Aware(stream=stream)
        assert stream_transport_of(g) is stream

    def test_returns_unified_when_only_unified_set(self):
        unified = _DummyTransport()
        g = _Aware(unified=unified)
        assert stream_transport_of(g) is unified

    def test_prefers_stream_over_unified(self):
        stream = _DummyTransport()
        unified = _DummyTransport()
        g = _Aware(stream=stream, unified=unified)
        assert stream_transport_of(g) is stream

    def test_raises_when_no_transport_set(self):
        g = _Aware()
        with pytest.raises(RuntimeError, match="no stream-capable transport"):
            stream_transport_of(g)
