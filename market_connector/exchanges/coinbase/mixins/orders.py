"""OrdersMixin: order placement, cancellation, and open-order listing via Coinbase REST API."""

from __future__ import annotations

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
from market_connector.exchanges.coinbase.schemas.rest import (
    CancelOrdersResponse,
    CreateOrderResponse,
    ListOrdersResponse,
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


def _exchange_ids(gateway: object) -> dict[str, str]:
    """Per-gateway client_order_id -> exchange order id map (created lazily).

    Filled by ``place_order``; entries are dropped on successful cancel.
    """
    return gateway.__dict__.setdefault("_exchange_order_ids", {})  # type: ignore[no-any-return]


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
        try:
            response = await self._rest.request("place_order", data=body)
        except (ExchangeUnavailableError, httpx.TransportError) as exc:
            # The POST may have reached the exchange; never re-send, let the caller resolve it.
            raise OrderOutcomeUnknownError(client_order_id=client_id) from exc
        parsed = CreateOrderResponse.model_validate(response.raw)
        if not parsed.success:
            raise OrderRejectedError(parsed.failure_reason or "order rejected")
        if parsed.order_id:
            _exchange_ids(self)[client_id] = parsed.order_id
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
        ids = _exchange_ids(self)
        exchange_id = ids.get(client_order_id)
        if exchange_id is None:
            found = await self.get_order_by_client_order_id(client_order_id)
            exchange_id = found.exchange_order_id if found is not None else None
            if exchange_id is None:
                raise OrderNotFoundError(f"no Coinbase order for client_order_id {client_order_id}")
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
        params: dict[str, str] = {}
        while True:
            response = await self._rest.request("list_orders", params=dict(params))
            parsed = ListOrdersResponse.model_validate(response.raw)
            for order in parsed.orders:
                if order.client_order_id == client_order_id:
                    return to_open_order(order)
            if not (parsed.has_next and parsed.cursor) or parsed.cursor == params.get("cursor"):
                return None
            params["cursor"] = parsed.cursor
