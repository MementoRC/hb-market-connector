"""OrdersMixin: order placement, cancellation, and open-order listing via Coinbase REST API."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import httpx

from market_connector.exceptions import (
    ExchangeUnavailableError,
    GatewayNotStartedError,
    OrderOutcomeUnknownError,
    OrderRejectedError,
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
    raise ValueError(f"Unsupported order type: {order_type}")


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
        cfg = _build_order_config(coerce_order_type(order_type), amount, price)
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
        return client_id

    async def cancel_order(
        self: HasRest & HasReady,  # type: ignore[valid-type]
        trading_pair: str,
        client_order_id: str,
    ) -> bool:
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")
        # NOTE: Coinbase /orders/batch_cancel expects exchange order IDs, not client_order_ids.
        # This implementation accepts client_order_id directly for the simple case where the
        # caller tracks the mapping. A follow-up should add a client→exchange ID map
        # populated by place_order and consumed here.
        response = await self._rest.request("cancel_orders", data={"order_ids": [client_order_id]})
        parsed = CancelOrdersResponse.model_validate(response.raw)
        return all(r.success for r in parsed.results)

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
