"""Unit tests for market_connector.exchanges.kraken.hb_compat.kraken_startup_cleanup."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from market_connector.exchanges.kraken.hb_compat.kraken_startup_cleanup import (
    reconcile_stale_orders,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_order(exchange_order_id: str | None = "TXID-0001") -> MagicMock:
    """Return a mock InFlightOrder-like object."""
    order = MagicMock()
    order.exchange_order_id = exchange_order_id
    order.cancel_attempted = False
    order.status = "open"
    return order


def _make_gateway(open_orders: dict) -> AsyncMock:
    """Return a mock KrakenGateway with get_open_orders returning open_orders."""
    gw = AsyncMock()
    gw.get_open_orders = AsyncMock(return_value=open_orders)
    return gw


def _make_connector(in_flight: dict) -> MagicMock:
    """Return a mock connector with in_flight_orders."""
    conn = MagicMock()
    conn.in_flight_orders = in_flight
    return conn


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestReconcileStaleOrdersEmptyTracker:
    async def test_empty_tracker_returns_zero(self) -> None:
        gw = _make_gateway({})
        conn = _make_connector({})
        result = await reconcile_stale_orders(gw, conn)
        assert result == 0

    async def test_empty_tracker_still_fetches_open_orders(self) -> None:
        gw = _make_gateway({})
        conn = _make_connector({})
        await reconcile_stale_orders(gw, conn)
        gw.get_open_orders.assert_awaited_once()


class TestReconcileStaleOrdersAllValid:
    async def test_all_valid_orders_untouched(self) -> None:
        """Orders present on exchange should not be reconciled."""
        open_orders = {"TXID-AAAA": MagicMock(), "TXID-BBBB": MagicMock()}
        orders = {
            "client-1": _make_order("TXID-AAAA"),
            "client-2": _make_order("TXID-BBBB"),
        }
        gw = _make_gateway(open_orders)
        conn = _make_connector(orders)
        result = await reconcile_stale_orders(gw, conn)
        assert result == 0
        # Neither order removed
        assert "client-1" in conn.in_flight_orders
        assert "client-2" in conn.in_flight_orders


class TestReconcileStaleOrdersMissingExchangeId:
    async def test_removes_orders_without_exchange_id(self) -> None:
        """Orders with exchange_order_id=None must be removed."""
        orders = {
            "client-no-id": _make_order(exchange_order_id=None),
            "client-valid": _make_order(exchange_order_id="TXID-GOOD"),
        }
        open_orders = {"TXID-GOOD": MagicMock()}
        gw = _make_gateway(open_orders)
        conn = _make_connector(orders)
        result = await reconcile_stale_orders(gw, conn)
        assert result == 1
        assert "client-no-id" not in conn.in_flight_orders
        assert "client-valid" in conn.in_flight_orders

    async def test_removes_orders_with_empty_string_exchange_id(self) -> None:
        orders = {"client-empty": _make_order(exchange_order_id="")}
        gw = _make_gateway({})
        conn = _make_connector(orders)
        result = await reconcile_stale_orders(gw, conn)
        assert result == 1
        assert "client-empty" not in conn.in_flight_orders


class TestReconcileStaleOrdersMissingOnExchange:
    async def test_marks_orders_not_on_exchange_as_cancelled(self) -> None:
        """Orders with an exchange_id not in open_orders should be marked cancelled."""
        orders = {"client-gone": _make_order("TXID-GONE")}
        gw = _make_gateway({})  # empty → TXID-GONE not present
        conn = _make_connector(orders)
        result = await reconcile_stale_orders(gw, conn)
        assert result == 1
        # Order still in tracker (just status updated)
        order = conn.in_flight_orders["client-gone"]
        assert order.status == "cancelled"

    async def test_mixed_valid_and_stale(self) -> None:
        """One valid, one missing, one no-id → 2 reconciled."""
        orders = {
            "valid": _make_order("TXID-OK"),
            "gone": _make_order("TXID-GONE"),
            "no-id": _make_order(None),
        }
        gw = _make_gateway({"TXID-OK": MagicMock()})
        conn = _make_connector(orders)
        result = await reconcile_stale_orders(gw, conn)
        assert result == 2
        assert "valid" in conn.in_flight_orders
        assert "no-id" not in conn.in_flight_orders
        assert conn.in_flight_orders["gone"].status == "cancelled"


class TestReconcileStaleOrdersGatewayException:
    async def test_gateway_exception_swallowed_returns_zero(self) -> None:
        """If gateway.get_open_orders raises, the function must not raise."""
        gw = AsyncMock()
        gw.get_open_orders = AsyncMock(side_effect=RuntimeError("Network error"))
        conn = _make_connector({"client-1": _make_order()})
        result = await reconcile_stale_orders(gw, conn)
        assert result == 0

    async def test_missing_in_flight_orders_attribute(self) -> None:
        """Connector without in_flight_orders attribute should degrade gracefully."""
        gw = _make_gateway({})
        conn = MagicMock(spec=[])  # no in_flight_orders attribute
        result = await reconcile_stale_orders(gw, conn)
        assert result == 0
