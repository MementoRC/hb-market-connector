"""OrdersMixin: order placement, cancellation, and open-order queries via Kraken REST API."""

from __future__ import annotations

from typing import TYPE_CHECKING

from market_connector.exceptions import GatewayNotStartedError
from market_connector.exchanges.kraken.errors import raise_on_kraken_error
from market_connector.exchanges.kraken.schemas.rest import (
    AddOrderResult,
    CancelOrderResult,
    KrakenResponse,
    OrderInfo,
    TradeInfo,
)
from market_connector.primitives import OrderType, TradeType

if TYPE_CHECKING:
    from decimal import Decimal

    from market_connector.exchanges.kraken.mixins.protocols import HasReady, HasRest

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_ORDER_TYPE_MAP: dict[str, str] = {
    "MARKET": "market",
    "LIMIT": "limit",
}


def _to_kraken_ordertype(order_type: OrderType | str) -> str:
    """Translate an OrderType enum or string to a Kraken ``ordertype`` string.

    Args:
        order_type: ``OrderType.MARKET``, ``OrderType.LIMIT``, or their
            string equivalents (case-insensitive).

    Raises:
        ValueError: For order types not supported at Stage 4 (conditional
            types are deferred to Stage 5 hb_compat).
    """
    key = order_type.name if isinstance(order_type, OrderType) else str(order_type).upper()
    if key not in _ORDER_TYPE_MAP:
        raise ValueError(
            f"Unsupported order type at Stage 4: {order_type!r}. "
            "Conditional order types (STOP_LOSS, TAKE_PROFIT, TRAILING_STOP) "
            "are mapped in Stage 5 hb_compat."
        )
    return _ORDER_TYPE_MAP[key]


def _to_kraken_side(side: TradeType | str) -> str:
    """Translate a TradeType enum or string to a Kraken side string.

    Kraken expects ``"buy"`` or ``"sell"`` (lowercase).
    """
    return side.name.lower() if isinstance(side, TradeType) else str(side).lower()


# ---------------------------------------------------------------------------
# Mixin
# ---------------------------------------------------------------------------


class OrdersMixin:
    async def place_order(
        self: HasRest & HasReady,  # type: ignore[valid-type]
        trading_pair: str,
        order_type: OrderType | str,
        side: TradeType | str,
        amount: Decimal,
        price: Decimal | None = None,
    ) -> str:
        """Place an order on Kraken and return the first txid.

        Args:
            trading_pair: Exchange-native pair string (e.g. ``"XBTUSD"``).
            order_type: ``OrderType.MARKET`` or ``OrderType.LIMIT`` (Stage 4).
            side: ``TradeType.BUY`` / ``TradeType.SELL`` or lowercase strings.
            amount: Order volume as a ``Decimal``.
            price: Limit price; required when ``order_type`` is ``LIMIT``.

        Returns:
            The first txid string from ``result.txid``.

        Raises:
            GatewayNotStartedError: If the gateway is not ready.
            ValueError: For unsupported order types or missing price on LIMIT.
        """
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")

        kraken_ordertype = _to_kraken_ordertype(order_type)
        if kraken_ordertype == "limit" and price is None:
            raise ValueError("price is required for LIMIT orders")

        body: dict[str, object] = {
            "pair": trading_pair,
            "type": _to_kraken_side(side),
            "ordertype": kraken_ordertype,
            "volume": str(amount),
        }
        if price is not None:
            body["price"] = str(price)

        response = await self._rest.request("add_order", data=body)
        envelope = KrakenResponse[AddOrderResult].model_validate(response.raw)
        raise_on_kraken_error(envelope.error)
        return envelope.result.txid[0]  # type: ignore[union-attr]

    async def cancel_order(
        self: HasRest & HasReady,  # type: ignore[valid-type]
        trading_pair: str,
        txid: str,
    ) -> bool:
        """Cancel an order by txid.

        Args:
            trading_pair: Not used by Kraken's cancel endpoint; present for
                interface parity with other connectors.
            txid: The Kraken transaction ID to cancel.

        Returns:
            ``True`` if ``result.count >= 1`` (order was cancelled).

        Raises:
            GatewayNotStartedError: If the gateway is not ready.
        """
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")

        response = await self._rest.request("cancel_order", data={"txid": txid})
        envelope = KrakenResponse[CancelOrderResult].model_validate(response.raw)
        raise_on_kraken_error(envelope.error)
        return envelope.result.count >= 1  # type: ignore[union-attr]

    async def get_open_orders(
        self: HasRest & HasReady,  # type: ignore[valid-type]
    ) -> dict[str, OrderInfo]:
        """Return all open orders keyed by txid.

        Kraken's ``/0/private/OpenOrders`` response wraps the order map under
        the ``"open"`` key: ``{"result": {"open": {<txid>: {...}}}}``.  This
        method unwraps that layer and validates each value as ``OrderInfo``.

        Raises:
            GatewayNotStartedError: If the gateway is not ready.
        """
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")

        response = await self._rest.request("open_orders")
        raw = response.raw
        raise_on_kraken_error(raw.get("error", []))
        open_dict: dict[str, object] = raw.get("result", {}).get("open", {})
        return {txid: OrderInfo.model_validate(info) for txid, info in open_dict.items()}

    async def query_orders(
        self: HasRest & HasReady,  # type: ignore[valid-type]
        txids: list[str],
    ) -> dict[str, OrderInfo]:
        """Batch-query orders by txid list.

        Args:
            txids: List of Kraken transaction IDs to look up.

        Returns:
            Dict mapping txid → ``OrderInfo``.

        Raises:
            GatewayNotStartedError: If the gateway is not ready.
        """
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")

        response = await self._rest.request("query_orders", data={"txid": ",".join(txids)})
        raw = response.raw
        raise_on_kraken_error(raw.get("error", []))
        result_dict: dict[str, object] = raw.get("result", {})
        return {txid: OrderInfo.model_validate(info) for txid, info in result_dict.items()}

    async def query_trades(
        self: HasRest & HasReady,  # type: ignore[valid-type]
        txids: list[str],
    ) -> dict[str, TradeInfo]:
        """Batch-query trades by txid list.

        Args:
            txids: List of Kraken trade IDs to look up.

        Returns:
            Dict mapping txid → ``TradeInfo``.

        Raises:
            GatewayNotStartedError: If the gateway is not ready.
        """
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")

        response = await self._rest.request("query_trades", data={"txid": ",".join(txids)})
        raw = response.raw
        raise_on_kraken_error(raw.get("error", []))
        result_dict: dict[str, object] = raw.get("result", {})
        return {txid: TradeInfo.model_validate(info) for txid, info in result_dict.items()}
