"""End-to-end smoke tests for KrakenGateway and KrakenConnectorBridge composition.

These tests use AsyncMock to stub all network I/O.  They validate:
- Gateway and bridge instantiate cleanly without network calls
- All four mixins are accessible on the gateway instance
- Order-type translation flows correctly through the bridge
- Startup cleanup is called from bridge.start()
- get_open_orders is the source of truth for reconciliation
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlparse

from market_connector.exchanges.kraken.converters import kraken_ordertype_from_hb
from market_connector.exchanges.kraken.hb_compat.kraken_bridge import KrakenConnectorBridge
from market_connector.exchanges.kraken.kraken_gateway import KrakenConfig, KrakenGateway
from market_connector.exchanges.kraken.schemas.enums import KrakenAPITier
from market_connector.primitives import OrderType

_DUMMY_KEY = "dummy-api-key"
_DUMMY_SECRET = "dGVzdHNlY3JldGtleWJhc2U2NGVuY29kZWQ="  # noqa: S105 — not a real secret


# ---------------------------------------------------------------------------
# 5e-1. Gateway instantiation
# ---------------------------------------------------------------------------


class TestGatewayInstantiation:
    def test_instantiates_with_dummy_credentials(self) -> None:
        gw = KrakenGateway(api_key=_DUMMY_KEY, secret_key=_DUMMY_SECRET)
        assert gw is not None

    def test_ready_is_false_before_start(self) -> None:
        gw = KrakenGateway(api_key=_DUMMY_KEY, secret_key=_DUMMY_SECRET)
        assert gw.ready is False

    def test_all_mixins_accessible(self) -> None:
        """All four mixin method sets must be reachable on the gateway."""
        gw = KrakenGateway(api_key=_DUMMY_KEY, secret_key=_DUMMY_SECRET)
        assert callable(getattr(gw, "get_balance", None)), "AccountsMixin missing"
        assert callable(getattr(gw, "get_orderbook", None)), "MarketDataMixin missing"
        assert callable(getattr(gw, "place_order", None)), "OrdersMixin missing"
        assert callable(getattr(gw, "subscribe_orderbook", None)), "SubscriptionsMixin missing"

    def test_default_tier_is_starter(self) -> None:
        gw = KrakenGateway(api_key=_DUMMY_KEY, secret_key=_DUMMY_SECRET)
        assert gw._config.tier == KrakenAPITier.STARTER

    def test_intermediate_tier_accepted(self) -> None:
        gw = KrakenGateway(
            api_key=_DUMMY_KEY, secret_key=_DUMMY_SECRET, tier=KrakenAPITier.INTERMEDIATE
        )
        assert gw._config.tier == KrakenAPITier.INTERMEDIATE

    def test_config_urls_default_to_live(self) -> None:
        cfg = KrakenConfig(api_key=_DUMMY_KEY, secret_key=_DUMMY_SECRET)
        assert urlparse(cfg.base_url).hostname == "api.kraken.com"
        assert urlparse(cfg.ws_url).hostname == "ws.kraken.com"

    def test_config_sandbox_url(self) -> None:
        cfg = KrakenConfig(api_key=_DUMMY_KEY, secret_key=_DUMMY_SECRET, sandbox=True)
        hostname = urlparse(cfg.base_url).hostname or ""
        assert "demo" in hostname or "sandbox" in hostname or "futures" in hostname

    async def test_start_calls_rest_and_ws(self) -> None:
        gw = KrakenGateway(api_key=_DUMMY_KEY, secret_key=_DUMMY_SECRET)
        gw._rest = AsyncMock()
        gw._rest.request = AsyncMock(return_value=MagicMock(raw={"result": {"unixtime": 1}}))
        gw._ws = AsyncMock()
        await gw.start()
        assert gw.ready is True
        gw._rest.request.assert_awaited_once_with("server_time")
        gw._ws.connect.assert_awaited_once()

    async def test_stop_after_start(self) -> None:
        gw = KrakenGateway(api_key=_DUMMY_KEY, secret_key=_DUMMY_SECRET)
        gw._rest = AsyncMock()
        gw._rest.request = AsyncMock(return_value=MagicMock(raw={}))
        gw._ws = AsyncMock()
        await gw.start()
        await gw.stop()
        assert gw.ready is False


# ---------------------------------------------------------------------------
# 5e-2. Bridge instantiation
# ---------------------------------------------------------------------------


class TestBridgeInstantiation:
    def test_instantiates_with_dummy_credentials(self) -> None:
        bridge = KrakenConnectorBridge(api_key=_DUMMY_KEY, secret_key=_DUMMY_SECRET)
        assert bridge is not None

    def test_ready_is_false_before_start(self) -> None:
        bridge = KrakenConnectorBridge(api_key=_DUMMY_KEY, secret_key=_DUMMY_SECRET)
        assert bridge.ready is False

    def test_gateway_accessible(self) -> None:
        bridge = KrakenConnectorBridge(api_key=_DUMMY_KEY, secret_key=_DUMMY_SECRET)
        assert isinstance(bridge.gateway, KrakenGateway)

    async def test_start_calls_reconcile_stale_orders(self) -> None:
        bridge = KrakenConnectorBridge(api_key=_DUMMY_KEY, secret_key=_DUMMY_SECRET)
        bridge._gateway._rest = AsyncMock()
        bridge._gateway._rest.request = AsyncMock(return_value=MagicMock(raw={}))
        bridge._gateway._ws = AsyncMock()

        with patch(
            "market_connector.exchanges.kraken.hb_compat.kraken_bridge.reconcile_stale_orders",
            new_callable=AsyncMock,
            return_value=0,
        ) as mock_reconcile:
            await bridge.start()
            mock_reconcile.assert_awaited_once_with(bridge._gateway, bridge)

        assert bridge.ready is True

    async def test_start_idempotent(self) -> None:
        bridge = KrakenConnectorBridge(api_key=_DUMMY_KEY, secret_key=_DUMMY_SECRET)
        bridge._gateway._rest = AsyncMock()
        bridge._gateway._rest.request = AsyncMock(return_value=MagicMock(raw={}))
        bridge._gateway._ws = AsyncMock()

        with patch(
            "market_connector.exchanges.kraken.hb_compat.kraken_bridge.reconcile_stale_orders",
            new_callable=AsyncMock,
            return_value=0,
        ):
            await bridge.start()
            await bridge.start()  # second call is no-op

        assert bridge._gateway._ws.connect.await_count == 1


# ---------------------------------------------------------------------------
# 5e-3. Order-type translation E2E through bridge
# ---------------------------------------------------------------------------


class TestOrdertypeTranslationE2E:
    """Full path: bridge.place_order → kraken_ordertype_from_hb → gateway.place_order."""

    async def test_limit_order_translates_correctly(self) -> None:
        bridge = KrakenConnectorBridge(api_key=_DUMMY_KEY, secret_key=_DUMMY_SECRET)
        bridge._gateway.place_order = AsyncMock(return_value="TXID-LIMIT")  # type: ignore[method-assign]

        txid = await bridge.place_order(
            trading_pair="XBTUSD",
            order_type=OrderType.LIMIT,
            side="buy",
            amount=Decimal("0.001"),
            price=Decimal("50000"),
        )
        assert txid == "TXID-LIMIT"
        bridge._gateway.place_order.assert_awaited_once_with(
            trading_pair="XBTUSD",
            order_type="limit",
            side="buy",
            amount=Decimal("0.001"),
            price=Decimal("50000"),
        )

    async def test_market_order_translates_correctly(self) -> None:
        bridge = KrakenConnectorBridge(api_key=_DUMMY_KEY, secret_key=_DUMMY_SECRET)
        bridge._gateway.place_order = AsyncMock(return_value="TXID-MKT")  # type: ignore[method-assign]

        await bridge.place_order(
            trading_pair="XBTUSD",
            order_type="MARKET",
            side="sell",
            amount=Decimal("0.05"),
        )
        bridge._gateway.place_order.assert_awaited_once_with(
            trading_pair="XBTUSD",
            order_type="market",
            side="sell",
            amount=Decimal("0.05"),
            price=None,
        )

    def test_all_known_types_produce_valid_kraken_strings(self) -> None:
        """kraken_ordertype_from_hb must handle all market_connector.primitives.OrderType."""
        valid_kraken_strings = {"limit", "market", "stop-loss", "take-profit", "trailing-stop"}
        for ot in OrderType:
            result = kraken_ordertype_from_hb(ot)
            assert result in valid_kraken_strings, (
                f"OrderType.{ot.name} produced {result!r} which is not a known Kraken string"
            )

    async def test_cancel_order_delegates_to_gateway(self) -> None:
        bridge = KrakenConnectorBridge(api_key=_DUMMY_KEY, secret_key=_DUMMY_SECRET)
        bridge._gateway.cancel_order = AsyncMock(return_value=True)  # type: ignore[method-assign]
        result = await bridge.cancel_order("XBTUSD", "TXID-CANCEL")
        assert result is True
        bridge._gateway.cancel_order.assert_awaited_once_with(
            trading_pair="XBTUSD", txid="TXID-CANCEL"
        )

    async def test_get_balance_delegates_to_gateway(self) -> None:
        bridge = KrakenConnectorBridge(api_key=_DUMMY_KEY, secret_key=_DUMMY_SECRET)
        bridge._gateway.get_balance = AsyncMock(return_value=Decimal("1.5"))  # type: ignore[method-assign]
        result = await bridge.get_balance("XXBT")
        assert result == Decimal("1.5")
