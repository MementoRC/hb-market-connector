# tests/test_phase_a_integration.py
"""Phase A integration smoke test: verify all framework additions wire up.

Constructs a fake unified-transport gateway with a ContractResolver and
exercises both transport dispatch helpers + native_for() in sequence.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from market_connector.auth.passthrough import PassThroughSigner
from market_connector.contracts.dispatch import native_for
from market_connector.contracts.instrument import InstrumentRef, InstrumentType
from market_connector.contracts.protocols import ContractResolver, ResolvedContract
from market_connector.protocols import TransportAwareGateway
from market_connector.transport.dispatch import (
    request_transport_of,
    stream_transport_of,
)

if TYPE_CHECKING:
    from market_connector.primitives import ConnectorPair


class _FakeTransport:
    """Conforms to Transport (intersection of RequestTransport + StreamTransport).

    Also exposes is_connected/connect/disconnect for the fake gateway's
    lifecycle (not protocol requirements; gateway-level concrete usage).
    """

    is_connected = True

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def request(self, method: str, *args: Any, **kwargs: Any) -> str:
        return f"REQ:{method}"

    def subscribe(self, channel: str, key: Any, callback: Any) -> None:
        return None


class _FakeResolver:
    async def resolve(self, ref: InstrumentRef) -> ResolvedContract:
        return ResolvedContract(ref=ref, native=f"NATIVE:{ref.symbol}", contract_id=ref.symbol)

    async def resolve_from_pair(self, pair: ConnectorPair) -> ResolvedContract:
        return ResolvedContract(
            ref=InstrumentRef(symbol=str(pair), instrument_type=InstrumentType.STOCK),
            native=f"PAIR_NATIVE:{pair}",
            contract_id=str(pair),
        )

    def invalidate(self, ref: InstrumentRef | None = None) -> None:
        pass


class _FakeIbGateway:
    """Minimal stub mimicking the eventual IbGatewayGateway shape."""

    def __init__(self) -> None:
        self.signer = PassThroughSigner()
        self.transport = _FakeTransport()
        self.rest_transport = None
        self.stream_transport = None
        self.unified_transport: _FakeTransport | None = self.transport
        self.contract_resolver: _FakeResolver | None = _FakeResolver()

    @property
    def ready(self) -> bool:
        # IbGatewayTransport has is_connected as a concrete attribute (not a
        # protocol requirement); access via concrete reference.
        return getattr(self.transport, "is_connected", False)

    async def start(self) -> None:
        await self.transport.connect()

    async def stop(self) -> None:
        await self.transport.disconnect()

    # ExchangeGateway protocol stubs (required for isinstance check)
    async def place_order(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def cancel_order(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def get_open_orders(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def get_balance(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def get_orderbook(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def get_candles(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def get_mid_price(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def subscribe_orderbook(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def subscribe_trades(self, *args: Any, **kwargs: Any) -> None:
        pass


class TestPhaseAIntegration:
    def test_fake_gateway_satisfies_transport_aware_protocol(self) -> None:
        g = _FakeIbGateway()
        assert isinstance(g, TransportAwareGateway)

    @pytest.mark.asyncio
    async def test_request_transport_routes_to_unified(self) -> None:
        g = _FakeIbGateway()
        rt = request_transport_of(g)
        assert rt is g.unified_transport
        result = await rt.request("placeOrder")
        assert result == "REQ:placeOrder"

    def test_stream_transport_routes_to_unified(self) -> None:
        g = _FakeIbGateway()
        st = stream_transport_of(g)
        assert st is g.unified_transport

    @pytest.mark.asyncio
    async def test_native_for_uses_resolver_with_instrument_ref(self) -> None:
        g = _FakeIbGateway()
        ref = InstrumentRef(symbol="ESM5", instrument_type=InstrumentType.FUTURE)
        native = await native_for(g, ref)
        assert native == "NATIVE:ESM5"

    @pytest.mark.asyncio
    async def test_native_for_uses_resolver_with_pair_string(self) -> None:
        g = _FakeIbGateway()
        # ConnectorPair is a type alias for str (Python 3.12 'type' statement),
        # not a callable class. Use a plain str literal — str IS ConnectorPair.
        pair: ConnectorPair = "AAPL-USD"
        native = await native_for(g, pair)
        assert native == "PAIR_NATIVE:AAPL-USD"

    def test_fake_gateway_satisfies_exchange_gateway_protocol(self) -> None:
        """Verify gw also isinstance-checks as ExchangeGateway (not just TransportAware)."""
        from market_connector.protocols import ExchangeGateway

        g = _FakeIbGateway()
        assert isinstance(g, ExchangeGateway)

    def test_contract_resolver_isinstance_check(self) -> None:
        """_FakeResolver structurally satisfies ContractResolver protocol."""
        resolver = _FakeResolver()
        assert isinstance(resolver, ContractResolver)
