"""Tests for TransportAwareGateway sub-protocol.

Verifies:
1. Existing ExchangeGateway is untouched (structural check).
2. TransportAwareGateway extends ExchangeGateway with new slots.
3. A class with all slots satisfies TransportAwareGateway.
4. A class without the new slots still satisfies plain ExchangeGateway.
"""

from __future__ import annotations

from market_connector.protocols import ExchangeGateway, TransportAwareGateway


class _MinimalGateway:
    """Satisfies plain ExchangeGateway only (no transport slots).

    ExchangeGateway is composed from ExecutionGateway + MarketDataGateway +
    lifecycle. @runtime_checkable isinstance checks attribute presence for
    ALL protocol members (including inherited ones), so this stub must
    implement every method to satisfy isinstance.
    """

    @property
    def ready(self) -> bool:
        return False

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    # ExecutionGateway methods
    async def place_order(self, *args, **kwargs):
        pass

    async def cancel_order(self, *args, **kwargs):
        pass

    async def get_open_orders(self, *args, **kwargs):
        pass

    async def get_balance(self, *args, **kwargs):
        pass

    # MarketDataGateway methods
    async def get_orderbook(self, *args, **kwargs):
        pass

    async def get_candles(self, *args, **kwargs):
        pass

    async def get_mid_price(self, *args, **kwargs):
        pass

    async def subscribe_orderbook(self, *args, **kwargs):
        pass

    async def subscribe_trades(self, *args, **kwargs):
        pass


class _TransportAwareGateway:
    """Satisfies TransportAwareGateway (all extension slots present)."""

    def __init__(self):
        self.rest_transport = None
        self.stream_transport = None
        self.unified_transport = None
        self.contract_resolver = None

    @property
    def ready(self) -> bool:
        return False

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def place_order(self, *args, **kwargs):
        pass

    async def cancel_order(self, *args, **kwargs):
        pass

    async def get_open_orders(self, *args, **kwargs):
        pass

    async def get_balance(self, *args, **kwargs):
        pass

    async def get_orderbook(self, *args, **kwargs):
        pass

    async def get_candles(self, *args, **kwargs):
        pass

    async def get_mid_price(self, *args, **kwargs):
        pass

    async def subscribe_orderbook(self, *args, **kwargs):
        pass

    async def subscribe_trades(self, *args, **kwargs):
        pass


class TestExchangeGatewayUnchanged:
    def test_minimal_class_satisfies_exchange_gateway(self):
        g = _MinimalGateway()
        assert isinstance(g, ExchangeGateway)

    def test_minimal_class_does_not_satisfy_transport_aware(self):
        g = _MinimalGateway()
        assert not isinstance(g, TransportAwareGateway)


class TestTransportAwareGateway:
    def test_aware_class_satisfies_both(self):
        g = _TransportAwareGateway()
        assert isinstance(g, ExchangeGateway)
        assert isinstance(g, TransportAwareGateway)

    def test_required_slots_documented(self):
        # Slots are: rest_transport, stream_transport, unified_transport, contract_resolver
        annotations = TransportAwareGateway.__annotations__
        assert "rest_transport" in annotations
        assert "stream_transport" in annotations
        assert "unified_transport" in annotations
        assert "contract_resolver" in annotations
