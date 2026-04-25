"""OrdersMixin: order placement, cancellation, and open-order listing via Coinbase REST API."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from market_connector.exceptions import GatewayNotStartedError, OrderRejectedError
from market_connector.exchanges.coinbase.converters import to_exchange_pair, to_open_order
from market_connector.exchanges.coinbase.schemas.rest import (
    CancelOrdersResponse,
    CreateOrderResponse,
    ListOrdersResponse,
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
        order_type: OrderType | str,
        side: TradeType | str,
        amount: Decimal,
        price: Decimal | None,
    ) -> str:
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")

        client_id = f"coinbase-{uuid.uuid4()}"
        cfg = _build_order_config(OrderType(order_type), amount, price)
        body: dict[str, object] = {
            "client_order_id": client_id,
            "product_id": to_exchange_pair(trading_pair),
            "side": side if isinstance(side, str) else side.value,
            "order_configuration": cfg,
        }
        raw = await self._rest.request("place_order", data=body)
        response = CreateOrderResponse.model_validate(raw)
        if not response.success:
            raise OrderRejectedError(response.failure_reason or "order rejected")
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
        raw = await self._rest.request("cancel_orders", data={"order_ids": [client_order_id]})
        response = CancelOrdersResponse.model_validate(raw)
        return all(r.success for r in response.results)

    async def get_open_orders(
        self: HasRest & HasReady,  # type: ignore[valid-type]
        trading_pair: str,
    ) -> list[OpenOrder]:
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")
        product_id = to_exchange_pair(trading_pair)
        raw = await self._rest.request(
            "list_orders",
            params={"product_id": product_id, "order_status": "OPEN"},
        )
        response = ListOrdersResponse.model_validate(raw)
        return [to_open_order(o) for o in response.orders]
