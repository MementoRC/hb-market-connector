"""OrdersMixin: order placement, cancellation, and open-order queries via Kraken REST API."""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING

from market_connector.exceptions import GatewayNotStartedError
from market_connector.exchanges.kraken.age_penalties import (
    amend_penalty,
    cancel_penalty,
    edit_penalty,
)
from market_connector.exchanges.kraken.errors import raise_on_kraken_error
from market_connector.exchanges.kraken.schemas.rest import (
    AddOrderBatchResult,
    AddOrderResult,
    AmendOrderResult,
    CancelOrderResult,
    EditOrderResult,
    KrakenResponse,
    OrderInfo,
    TradeInfo,
)
from market_connector.exchanges.kraken.specs import KRAKEN_RATE_LIMIT_SPEC
from market_connector.primitives import OrderType, TradeType
from market_connector.rate_limits.limiter import ensure_keyed_limit, ensure_multi_keyed_limit

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from decimal import Decimal

    from market_connector.exchanges.kraken.mixins.protocols import HasReady, HasRest
    from market_connector.transport.response import Response
    from market_connector.transport.rest_base import RestConnectorBase

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


async def _request_keyed(
    rest: RestConnectorBase,
    endpoint_name: str,
    pair: str,
    body: dict[str, object],
    matching_units: int | None = None,
) -> Response:
    """Send ``endpoint_name`` charged to the per-pair matching limit when supported.

    ``matching_units`` overrides the units charged to the pair's matching counter;
    0 sends the request without charging it (hb-rate-limit rejects zero weights, so
    the unkeyed endpoint is used, which never touches ``matching:{pair}``).
    REST clients without a ``rate_limiter`` (e.g. test doubles) are called unkeyed.
    """
    limiter = getattr(rest, "rate_limiter", None)
    if limiter is None or matching_units == 0:
        return await rest.request(endpoint_name, data=body)
    limit_id = ensure_keyed_limit(
        limiter, endpoint_name, pair, KRAKEN_RATE_LIMIT_SPEC.keyed_endpoint_pools[endpoint_name]
    )
    weights = None if matching_units is None else {f"matching:{pair}": matching_units}
    return await rest.request(endpoint_name, data=body, limit_id=limit_id, weights=weights)


async def _request_multi_keyed(
    rest: RestConnectorBase,
    endpoint_name: str,
    matching_units: dict[str, int],
    body: dict[str, object],
) -> Response:
    """Send ``endpoint_name`` charging ``matching_units`` (pair -> units) to each pair's pool.

    Pairs with 0 units are not charged; with none left the unkeyed endpoint is used.
    """
    charged = {pair: units for pair, units in matching_units.items() if units > 0}
    limiter = getattr(rest, "rate_limiter", None)
    if limiter is None or not charged:
        return await rest.request(endpoint_name, data=body)
    limit_id = ensure_multi_keyed_limit(
        limiter, endpoint_name, charged, KRAKEN_RATE_LIMIT_SPEC.keyed_endpoint_pools[endpoint_name]
    )
    weights = {f"matching:{pair}": units for pair, units in charged.items()}
    return await rest.request(endpoint_name, data=body, limit_id=limit_id, weights=weights)


def _placed_at(owner: object) -> dict[str, float]:
    """The owner's txid -> placement-time map, created on first use (mixins are stateless)."""
    return vars(owner).setdefault("_order_placed_at", {})  # type: ignore[no-any-return]


def _age(owner: OrdersMixin, txid: str) -> float:
    """Seconds since this process placed ``txid``; 0 (worst-case penalty) if unknown."""
    placed = _placed_at(owner).get(txid)
    return 0.0 if placed is None else owner._order_clock() - placed


# ---------------------------------------------------------------------------
# Mixin
# ---------------------------------------------------------------------------


class OrdersMixin:
    # Monotonic seconds source for order ageing; tests inject a manual clock.
    _order_clock: Callable[[], float] = staticmethod(time.monotonic)

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

        response = await _request_keyed(self._rest, "add_order", trading_pair, body)
        envelope = KrakenResponse[AddOrderResult].model_validate(response.raw)
        raise_on_kraken_error(envelope.error)
        placed = self._order_clock()
        for txid in envelope.result.txid:  # type: ignore[union-attr]
            _placed_at(self)[txid] = placed
        return envelope.result.txid[0]  # type: ignore[union-attr]

    async def cancel_order(
        self: HasRest & HasReady,  # type: ignore[valid-type]
        trading_pair: str,
        txid: str,
    ) -> bool:
        """Cancel an order by txid.

        Args:
            trading_pair: Not sent to Kraken's cancel endpoint; keys the per-pair
                matching rate limit.
            txid: The Kraken transaction ID to cancel.

        Returns:
            ``True`` if ``result.count >= 1`` (order was cancelled).

        Raises:
            GatewayNotStartedError: If the gateway is not ready.

        The matching counter is charged the order-age penalty. The age comes from
        this process's ``place_order``; an unknown txid is charged the worst case.
        """
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")

        placed = _placed_at(self).get(txid)
        units = cancel_penalty(0.0 if placed is None else self._order_clock() - placed)
        response = await _request_keyed(
            self._rest, "cancel_order", trading_pair, {"txid": txid}, units
        )
        envelope = KrakenResponse[CancelOrderResult].model_validate(response.raw)
        raise_on_kraken_error(envelope.error)
        _placed_at(self).pop(txid, None)
        return envelope.result.count >= 1  # type: ignore[union-attr]

    async def amend_order(
        self: HasRest & HasReady,  # type: ignore[valid-type]
        trading_pair: str,
        txid: str,
        *,
        amount: Decimal | None = None,
        price: Decimal | None = None,
    ) -> str:
        """Amend an order in place (keeps its txid) and return the ``amend_id``.

        Unset ``amount`` / ``price`` are omitted from the request. The matching
        counter is charged the amend penalty for the order's age, which then restarts.

        Raises:
            GatewayNotStartedError: If the gateway is not ready.
        """
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")

        body: dict[str, object] = {"txid": txid}
        if amount is not None:
            body["order_qty"] = str(amount)
        if price is not None:
            body["limit_price"] = str(price)
        units = amend_penalty(_age(self, txid))
        response = await _request_keyed(self._rest, "amend_order", trading_pair, body, units)
        envelope = KrakenResponse[AmendOrderResult].model_validate(response.raw)
        raise_on_kraken_error(envelope.error)
        _placed_at(self)[txid] = self._order_clock()
        return envelope.result.amend_id  # type: ignore[union-attr]

    async def edit_order(
        self: HasRest & HasReady,  # type: ignore[valid-type]
        trading_pair: str,
        txid: str,
        *,
        amount: Decimal | None = None,
        price: Decimal | None = None,
    ) -> str:
        """Edit an order (cancel and replace) and return the new txid.

        Unset ``amount`` / ``price`` are omitted from the request. The matching
        counter is charged the edit penalty for the order's age; the old txid is
        forgotten and the new one starts ageing now.

        Raises:
            GatewayNotStartedError: If the gateway is not ready.
        """
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")

        body: dict[str, object] = {"txid": txid, "pair": trading_pair}
        if amount is not None:
            body["volume"] = str(amount)
        if price is not None:
            body["price"] = str(price)
        units = edit_penalty(_age(self, txid))
        response = await _request_keyed(self._rest, "edit_order", trading_pair, body, units)
        envelope = KrakenResponse[EditOrderResult].model_validate(response.raw)
        raise_on_kraken_error(envelope.error)
        _placed_at(self).pop(txid, None)
        new_txid = envelope.result.txid  # type: ignore[union-attr]
        _placed_at(self)[new_txid] = self._order_clock()
        return new_txid

    async def place_orders_batch(
        self: HasRest & HasReady,  # type: ignore[valid-type]
        trading_pair: str,
        orders: Sequence[dict[str, object]],
    ) -> list[str]:
        """Place 2 to 15 orders on one pair in a single request; return their txids.

        Args:
            trading_pair: Exchange-native pair shared by every order.
            orders: Kraken-native order dicts (``type``, ``ordertype``, ``volume``,
                optional ``price``), sent as given.

        The matching counter is charged ``ceil(len(orders) / 2)``.

        Raises:
            GatewayNotStartedError: If the gateway is not ready.
            ValueError: If there are not 2 to 15 orders (nothing is sent).
        """
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")
        if not 2 <= len(orders) <= 15:
            raise ValueError(f"A batch takes 2 to 15 orders, got {len(orders)}")

        body: dict[str, object] = {"pair": trading_pair, "orders": list(orders)}
        response = await _request_keyed(
            self._rest, "add_order_batch", trading_pair, body, math.ceil(len(orders) / 2)
        )
        envelope = KrakenResponse[AddOrderBatchResult].model_validate(response.raw)
        raise_on_kraken_error(envelope.error)
        placed = self._order_clock()
        txids = [entry.txid for entry in envelope.result.orders]  # type: ignore[union-attr]
        for txid in txids:
            _placed_at(self)[txid] = placed
        return txids

    async def cancel_orders_batch(
        self: HasRest & HasReady,  # type: ignore[valid-type]
        orders: Sequence[tuple[str, str]],
    ) -> int:
        """Cancel up to 50 ``(trading_pair, txid)`` orders in a single request.

        Returns:
            The number of orders cancelled (``result.count``).

        Each pair's matching counter is charged the sum of its orders' cancel
        penalties; a pair whose sum is 0 is not charged.

        Raises:
            GatewayNotStartedError: If the gateway is not ready.
            ValueError: If there are not 1 to 50 orders (nothing is sent).
        """
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")
        if not 1 <= len(orders) <= 50:
            raise ValueError(f"A cancel batch takes 1 to 50 orders, got {len(orders)}")

        units: dict[str, int] = {}
        for pair, txid in orders:
            units[pair] = units.get(pair, 0) + cancel_penalty(_age(self, txid))
        body: dict[str, object] = {"orders": [txid for _, txid in orders]}
        response = await _request_multi_keyed(self._rest, "cancel_order_batch", units, body)
        envelope = KrakenResponse[CancelOrderResult].model_validate(response.raw)
        raise_on_kraken_error(envelope.error)
        for _, txid in orders:
            _placed_at(self).pop(txid, None)
        return envelope.result.count  # type: ignore[union-attr]

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
