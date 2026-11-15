"""Static conformance mirror for #243 (checked by mypy, never imported at runtime by pytest).

Mirrors the shape strategy-framework PR #78 gives ``MarketAccessProtocol.place_order``
(``order_type: dtp.common.OrderType, side: dtp.common.TradeType``). Both market-connector
implementers must accept at least those types. Run:

    pixi run mypy tests/unit/_place_order_protocol.py

NOTE: ``pixi run typecheck`` is ``mypy market_connector scripts`` and does NOT cover tests/;
the GREEN change must extend that task to include this file.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from decimal import Decimal

    from data_type_primitives.common import OrderType, TradeType

    from market_connector.hb_compat.bridge import LiveMarketAccess as BridgeMarketAccess
    from market_connector.live_market_access import LiveMarketAccess as ConnectorMarketAccess


class _PlaceOrderLike(Protocol):
    def __call__(
        self,
        order_type: OrderType,
        side: TradeType,
        amount: Decimal,
        price: Decimal,
    ) -> str: ...


def _bridge_conforms(bridge: BridgeMarketAccess) -> None:
    _: _PlaceOrderLike = bridge.place_order


def _connector_conforms(access: ConnectorMarketAccess) -> None:
    _: _PlaceOrderLike = access.place_order
