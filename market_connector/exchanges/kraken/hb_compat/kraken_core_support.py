"""Pure helpers for :class:`KrakenConnectorCore` (no I/O, no hummingbot)."""

from __future__ import annotations

from collections import OrderedDict
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING

from data_type_primitives.common import TradeType
from data_type_primitives.in_flight_order import TradeUpdate
from data_type_primitives.trade_fee import TokenAmount, TradeFeeBase, TradeFeeSchema

from market_connector.exchanges.kraken.pairs import (
    TradingRule as TradingRule,  # noqa: TC001 - re-export; moved to kraken.pairs (#213)
)
from market_connector.exchanges.kraken.pairs import (
    build_pair_tables as build_pair_tables,  # re-export; moved to kraken.pairs (#213)
)
from market_connector.exchanges.kraken.pairs import (
    hb_pair_from_wsname as hb_pair_from_wsname,  # re-export; moved to kraken.pairs (#213)
)
from market_connector.hb_compat.order_events import (
    BuyOrderCompletedEvent,
    BuyOrderCreatedEvent,
    OrderFilledEvent,
    SellOrderCompletedEvent,
    SellOrderCreatedEvent,
)

if TYPE_CHECKING:
    from data_type_primitives import TradingRules
    from data_type_primitives.in_flight_order import InFlightOrder

    from market_connector.exchanges.kraken.schemas.rest import TradeInfo
    from market_connector.exchanges.kraken.schemas.ws import OwnTradeDetail

NAN = Decimal("NaN")


def round_down(value: Decimal, increment: Decimal) -> Decimal:
    """Round ``value`` down to a multiple of ``increment``.

    NaN, infinities and a non-positive ``increment`` (no constraint) pass unchanged.
    """
    if not value.is_finite() or increment <= 0:
        return value
    return (value / increment).to_integral_value(rounding=ROUND_DOWN) * increment


def quantize_amount(rule: TradingRules | None, amount: Decimal) -> Decimal:
    """Round ``amount`` down to the lot size; 0 when unknown, NaN or below the minimum size."""
    if rule is None or amount.is_nan():
        return Decimal(0)
    quantized = round_down(amount, rule.min_base_amount_increment)
    return quantized if quantized >= rule.min_order_size else Decimal(0)


def quantize_order(
    rule: TradingRules | None, amount: Decimal, price: Decimal
) -> tuple[Decimal, Decimal]:
    """Amount (lot) and price (tick) rounded down for AddOrder; unchanged without a rule."""
    if rule is None:
        return amount, price
    return (
        round_down(amount, rule.min_base_amount_increment),
        round_down(price, rule.min_price_increment),
    )


def build_trade_update(
    order: InFlightOrder,
    *,
    trade_id: str,
    price: Decimal,
    amount: Decimal,
    cost: Decimal,
    fee: Decimal,
    timestamp: float,
) -> TradeUpdate:
    """A fill for ``order``; Kraken charges the fee in the quote asset."""
    trade_fee = TradeFeeBase.new_spot_fee(
        TradeFeeSchema(),
        order.trade_type,
        flat_fees=[TokenAmount(order.quote_asset, fee)],
    )
    return TradeUpdate(
        trade_id=trade_id,
        client_order_id=order.client_order_id,
        exchange_order_id=order.exchange_order_id or "",
        trading_pair=order.trading_pair,
        fill_timestamp=timestamp,
        fill_price=price,
        fill_base_amount=amount,
        fill_quote_amount=cost,
        fee=trade_fee,
    )


def update_from_ws(order: InFlightOrder, trade_id: str, detail: OwnTradeDetail) -> TradeUpdate:
    """Fill from an ``ownTrades`` entry (its ``time`` is a string)."""
    return build_trade_update(
        order,
        trade_id=trade_id,
        price=Decimal(detail.price),
        amount=Decimal(detail.vol),
        cost=Decimal(detail.cost),
        fee=Decimal(detail.fee),
        timestamp=float(detail.time),
    )


def update_from_rest(order: InFlightOrder, trade_id: str, trade: TradeInfo) -> TradeUpdate:
    """Fill from a QueryTrades entry (its ``time`` is a float)."""
    return build_trade_update(
        order,
        trade_id=trade_id,
        price=Decimal(trade.price),
        amount=Decimal(trade.vol),
        cost=Decimal(trade.cost),
        fee=Decimal(trade.fee),
        timestamp=float(trade.time),
    )


# ---------------------------------------------------------------------------
# Event builders
# ---------------------------------------------------------------------------


def created_event(
    order: InFlightOrder, txid: str, now: float
) -> BuyOrderCreatedEvent | SellOrderCreatedEvent:
    cls = BuyOrderCreatedEvent if order.trade_type is TradeType.BUY else SellOrderCreatedEvent
    return cls(
        timestamp=now,
        type=order.order_type,
        trading_pair=order.trading_pair,
        amount=order.amount,
        price=NAN if order.price is None else order.price,
        order_id=order.client_order_id,
        creation_timestamp=order.creation_timestamp,
        exchange_order_id=txid,
        leverage=order.leverage,
        position=order.position.value,
    )


def completed_event(
    order: InFlightOrder, now: float
) -> BuyOrderCompletedEvent | SellOrderCompletedEvent:
    cls = BuyOrderCompletedEvent if order.trade_type is TradeType.BUY else SellOrderCompletedEvent
    return cls(
        timestamp=now,
        order_id=order.client_order_id,
        base_asset=order.base_asset,
        quote_asset=order.quote_asset,
        base_asset_amount=order.executed_amount_base,
        quote_asset_amount=order.executed_amount_quote,
        order_type=order.order_type,
        exchange_order_id=order.exchange_order_id,
    )


def filled_event(order: InFlightOrder, update: TradeUpdate) -> OrderFilledEvent:
    return OrderFilledEvent(
        timestamp=update.fill_timestamp,
        order_id=order.client_order_id,
        trading_pair=order.trading_pair,
        trade_type=order.trade_type,
        order_type=order.order_type,
        price=update.fill_price,
        amount=update.fill_base_amount,
        trade_fee=update.fee,
        exchange_trade_id=update.trade_id,
        exchange_order_id=update.exchange_order_id,
        leverage=order.leverage,
        position=order.position.value,
    )


# ---------------------------------------------------------------------------
# WS fills that arrive before AddOrder returned the txid
# ---------------------------------------------------------------------------


class PendingTrades:
    """Bounded, TTL-limited buffer of ``ownTrades`` entries for txids not yet known."""

    def __init__(self, ttl: float = 60.0, max_entries: int = 1000) -> None:
        self._ttl = ttl
        self._max = max_entries
        self._items: OrderedDict[tuple[str, str], tuple[float, OwnTradeDetail]] = OrderedDict()

    def add(self, trade_id: str, detail: OwnTradeDetail, now: float) -> None:
        self._expire(now)
        key = (detail.ordertxid, trade_id)
        self._items.pop(key, None)  # re-insert so insertion order matches timestamp order
        self._items[key] = (now, detail)
        while len(self._items) > self._max:
            self._items.popitem(last=False)

    def pop_for(self, txid: str, now: float) -> list[tuple[str, OwnTradeDetail]]:
        """Remove and return the live entries for ``txid`` as ``(trade_id, detail)``."""
        self._expire(now)
        keys = [key for key in self._items if key[0] == txid]
        return [(key[1], self._items.pop(key)[1]) for key in keys]

    def _expire(self, now: float) -> None:
        while self._items and now - next(iter(self._items.values()))[0] > self._ttl:
            self._items.popitem(last=False)
