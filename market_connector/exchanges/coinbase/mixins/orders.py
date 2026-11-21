"""OrdersMixin: order placement, cancellation, and open-order listing via Coinbase REST API."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from enum import Enum
from typing import TYPE_CHECKING

import httpx

from market_connector.exceptions import (
    ExchangeUnavailableError,
    GatewayNotStartedError,
    OrderNotFoundError,
    OrderOutcomeUnknownError,
    OrderRejectedError,
    UnsupportedOrderError,
)
from market_connector.exchanges.coinbase.converters import to_exchange_pair, to_open_order
from market_connector.exchanges.coinbase.schemas.enums import CoinbaseOrderStatus
from market_connector.exchanges.coinbase.schemas.rest import (
    CancelOrdersResponse,
    CreateOrderResponse,
    ListOrdersResponse,
    Order,
)
from market_connector.hb_compat.primitives import (
    CanonicalOrderType,
    CanonicalTradeType,
    coerce_order_type,
    coerce_trade_type,
)
from market_connector.primitives import OpenOrder, OrderType, TradeType

if TYPE_CHECKING:
    from decimal import Decimal

    from market_connector.exchanges.coinbase.mixins.protocols import HasReady, HasRest


def _build_order_config(
    order_type: OrderType, amount: Decimal, price: Decimal | None
) -> dict[str, object]:
    base_size = str(amount)
    if order_type == OrderType.LIMIT:
        return {
            "limit_limit_gtc": {
                "base_size": base_size,
                "limit_price": str(price),
                "post_only": False,
            }
        }
    if order_type == OrderType.LIMIT_MAKER:
        return {
            "limit_limit_gtc": {
                "base_size": base_size,
                "limit_price": str(price),
                "post_only": True,
            }
        }
    if order_type == OrderType.MARKET:
        return {"market_market_ioc": {"base_size": base_size}}
    raise UnsupportedOrderError(
        exchange="coinbase",
        order_type=getattr(order_type, "name", str(order_type)),
        reason="order type not supported by Coinbase",
    )


def _coerce_supported_order_type(value: object) -> OrderType:
    """Coerce to a local OrderType; canonical types with no local member are unsupported."""
    try:
        return coerce_order_type(value)
    except ValueError:
        name = value.name if isinstance(value, Enum) else str(value).strip().upper()
        if name.replace("-", "_") in CanonicalOrderType.__members__:
            raise UnsupportedOrderError(
                exchange="coinbase",
                order_type=name,
                reason="order type not supported by Coinbase",
            ) from None
        raise


_MAX_CACHED_IDS = 4096
# Upper bound (seconds) a cancel waits for an in-flight placement of the same client id.
_PLACEMENT_WAIT_TIMEOUT_S = 30.0
_TERMINAL_STATUSES = frozenset(
    {
        CoinbaseOrderStatus.FILLED,
        CoinbaseOrderStatus.CANCELLED,
        CoinbaseOrderStatus.EXPIRED,
        CoinbaseOrderStatus.FAILED,
    }
)


def _exchange_ids(gateway: object) -> dict[str, tuple[str, str]]:
    """Per-gateway client_order_id -> (exchange order id, product id) map (created lazily).

    Filled by ``place_order``; entries are dropped on successful cancel, when a lookup
    sees the order in a terminal state, and (oldest first) beyond ``_MAX_CACHED_IDS``.
    """
    return gateway.__dict__.setdefault("_exchange_order_ids", {})  # type: ignore[no-any-return]


def _remember_exchange_id(gateway: object, client_id: str, exchange_id: str, product: str) -> None:
    ids = _exchange_ids(gateway)
    ids.pop(client_id, None)
    ids[client_id] = (exchange_id, product)
    while len(ids) > _MAX_CACHED_IDS:
        ids.pop(next(iter(ids)))


def _placing(gateway: object) -> dict[str, asyncio.Event]:
    """In-flight placements by client_order_id; set when the placement resolves."""
    return gateway.__dict__.setdefault("_placing_events", {})  # type: ignore[no-any-return]


async def _find_raw_order(gateway: HasRest, client_order_id: str) -> Order | None:
    """Scan the order listing (all pages) for ``client_order_id``; no conversion."""
    params: dict[str, str] = {}
    while True:
        response = await gateway._rest.request("list_orders", params=dict(params))
        parsed = ListOrdersResponse.model_validate(response.raw)
        for order in parsed.orders:
            if order.client_order_id == client_order_id:
                return order
        if not (parsed.has_next and parsed.cursor) or parsed.cursor == params.get("cursor"):
            return None
        params["cursor"] = parsed.cursor


class OrdersMixin:
    async def place_order(
        self: HasRest & HasReady,  # type: ignore[valid-type]
        trading_pair: str,
        order_type: OrderType | CanonicalOrderType | str,
        side: TradeType | CanonicalTradeType | str,
        amount: Decimal,
        price: Decimal | None,
        *,
        client_order_id: str | None = None,
    ) -> str:
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")

        client_id = client_order_id or f"coinbase-{uuid.uuid4()}"
        cfg = _build_order_config(_coerce_supported_order_type(order_type), amount, price)
        body: dict[str, object] = {
            "client_order_id": client_id,
            "product_id": to_exchange_pair(trading_pair),
            "side": coerce_trade_type(side).value,
            "order_configuration": cfg,
        }
        inflight = asyncio.Event()
        _placing(self)[client_id] = inflight
        try:
            try:
                response = await self._rest.request("place_order", data=body)
            except (ExchangeUnavailableError, httpx.TransportError) as exc:
                # The POST may have reached the exchange; never re-send, let the caller resolve it.
                raise OrderOutcomeUnknownError(client_order_id=client_id) from exc
            parsed = CreateOrderResponse.model_validate(response.raw)
            if not parsed.success:
                raise OrderRejectedError(parsed.failure_reason or "order rejected")
            success = parsed.success_response
            exchange_id = parsed.order_id or (success.order_id if success else None)
            if exchange_id:
                product = success.product_id if success and success.product_id else None
                _remember_exchange_id(
                    self, client_id, exchange_id, product or str(body["product_id"])
                )
        finally:
            # Only drop our own registration: a concurrent placement with the same
            # client id may have replaced it, and its event must stay reachable.
            if _placing(self).get(client_id) is inflight:
                del _placing(self)[client_id]
            inflight.set()
        return client_id

    async def cancel_order(
        self: HasRest & HasReady,  # type: ignore[valid-type]
        trading_pair: str,
        client_order_id: str,
    ) -> bool:
        """Cancel by client order id; Coinbase batch_cancel needs the exchange order id.

        The exchange id comes from the map filled by ``place_order`` (entry dropped on
        successful cancel), else from ``get_order_by_client_order_id``. Raises
        ``OrderNotFoundError`` (sending nothing) when it cannot be resolved. Returns True
        only if the exchange reports success for that id.
        """
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")
        pending = _placing(self).get(client_order_id)
        if pending is not None:
            # A placement for this id is in flight: wait so its exchange id is cached.
            # Bounded; on timeout fall through to the normal resolution path.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(pending.wait(), timeout=_PLACEMENT_WAIT_TIMEOUT_S)
        ids = _exchange_ids(self)
        product_id = to_exchange_pair(trading_pair)
        cached = ids.get(client_order_id)
        if cached is not None:
            exchange_id, product = cached
        else:
            found = await _find_raw_order(self, client_order_id)
            if found is None:
                raise OrderNotFoundError(f"no Coinbase order for client_order_id {client_order_id}")
            exchange_id, product = found.order_id, found.product_id
        if product != product_id:
            raise OrderNotFoundError(
                f"Coinbase order for client_order_id {client_order_id} is on {product}, "
                f"not {product_id}"
            )
        response = await self._rest.request("cancel_orders", data={"order_ids": [exchange_id]})
        parsed = CancelOrdersResponse.model_validate(response.raw)
        ok = any(r.success and r.order_id == exchange_id for r in parsed.results)
        if ok:
            ids.pop(client_order_id, None)
        return ok

    async def get_open_orders(
        self: HasRest & HasReady,  # type: ignore[valid-type]
        trading_pair: str,
    ) -> list[OpenOrder]:
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")
        product_id = to_exchange_pair(trading_pair)
        response = await self._rest.request(
            "list_orders",
            params={"product_id": product_id, "order_status": "OPEN"},
        )
        parsed = ListOrdersResponse.model_validate(response.raw)
        return [to_open_order(o) for o in parsed.orders]

    async def get_order_by_client_order_id(
        self: HasRest & HasReady,  # type: ignore[valid-type]
        client_order_id: str,
    ) -> OpenOrder | None:
        """Find an order (any status) by ``client_order_id`` via the order listing, or None."""
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")
        order = await _find_raw_order(self, client_order_id)
        if order is None:
            return None
        if order.status in _TERMINAL_STATUSES:
            _exchange_ids(self).pop(client_order_id, None)
        return to_open_order(order)
