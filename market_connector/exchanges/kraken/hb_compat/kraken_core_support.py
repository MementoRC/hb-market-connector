"""Pure helpers for :class:`KrakenConnectorCore` (no I/O, no hummingbot)."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING

from data_type_primitives.common import TradeType
from data_type_primitives.in_flight_order import TradeUpdate
from data_type_primitives.trade_fee import TokenAmount, TradeFeeBase, TradeFeeSchema

from market_connector.exchanges.kraken.specs import KRAKEN_SYMBOL_MAPPER
from market_connector.hb_compat.logging import get_logger
from market_connector.hb_compat.order_events import (
    BuyOrderCompletedEvent,
    BuyOrderCreatedEvent,
    OrderFilledEvent,
    SellOrderCompletedEvent,
    SellOrderCreatedEvent,
)

if TYPE_CHECKING:
    from data_type_primitives.in_flight_order import InFlightOrder

    from market_connector.exchanges.kraken.schemas.rest import AssetPairInfo, TradeInfo
    from market_connector.exchanges.kraken.schemas.ws import OwnTradeDetail

logger = get_logger(__name__)

NAN = Decimal("NaN")


@dataclass(frozen=True)
class TradingRule:
    """Per-pair order limits derived from Kraken ``AssetPairs``."""

    trading_pair: str
    min_order_size: Decimal
    min_notional_size: Decimal
    amount_decimals: int
    price_decimals: int


def hb_pair_from_wsname(wsname: str) -> str | None:
    """``"XBT/USD"`` -> ``"BTC-USD"`` via the per-asset aliases; ``None`` if not ``BASE/QUOTE``."""
    parts = wsname.split("/")
    if len(parts) != 2 or not all(parts):  # noqa: PLR2004
        return None
    base, quote = (KRAKEN_SYMBOL_MAPPER.from_exchange_asset(p) for p in parts)
    return f"{base}-{quote}"


def build_pair_tables(
    asset_pairs: dict[str, AssetPairInfo],
) -> tuple[dict[str, TradingRule], dict[str, str]]:
    """Return ``(rules, wire_pairs)`` keyed by hummingbot pair; wire pair is the ``altname``.

    Entries without a usable ``wsname`` are skipped; for duplicates the first one wins.
    """
    rules: dict[str, TradingRule] = {}
    wire_pairs: dict[str, str] = {}
    for info in asset_pairs.values():
        hb_pair = hb_pair_from_wsname(info.wsname)
        if hb_pair is None or not info.altname:
            continue
        if hb_pair in wire_pairs:
            logger.debug("duplicate AssetPairs entry for %s (%s) ignored", hb_pair, info.altname)
            continue
        wire_pairs[hb_pair] = info.altname
        rules[hb_pair] = TradingRule(
            trading_pair=hb_pair,
            min_order_size=Decimal(info.ordermin),
            min_notional_size=Decimal(info.costmin),
            amount_decimals=info.lot_decimals,
            price_decimals=info.pair_decimals,
        )
    return rules, wire_pairs


def round_down(value: Decimal, decimals: int) -> Decimal:
    """Round ``value`` down to ``decimals`` places; shorter values and NaN pass unchanged."""
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int) or -exponent <= decimals:
        return value
    return value.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_DOWN)


def quantize_amount(rule: TradingRule | None, amount: Decimal) -> Decimal:
    """Round ``amount`` down to the lot size; 0 when unknown, NaN or below the minimum size."""
    if rule is None or amount.is_nan():
        return Decimal(0)
    quantized = round_down(amount, rule.amount_decimals)
    return quantized if quantized >= rule.min_order_size else Decimal(0)


def quantize_order(
    rule: TradingRule | None, amount: Decimal, price: Decimal
) -> tuple[Decimal, Decimal]:
    """Amount (lot) and price (tick) rounded down for AddOrder; unchanged without a rule."""
    if rule is None:
        return amount, price
    return round_down(amount, rule.amount_decimals), round_down(price, rule.price_decimals)


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
