"""Pure helpers for :class:`KrakenConnectorCore` (no I/O, no hummingbot)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING

from data_type_primitives.in_flight_order import TradeUpdate
from data_type_primitives.trade_fee import TokenAmount, TradeFeeBase, TradeFeeSchema

from market_connector.exchanges.kraken.specs import KRAKEN_SYMBOL_MAPPER

if TYPE_CHECKING:
    from data_type_primitives.in_flight_order import InFlightOrder

    from market_connector.exchanges.kraken.schemas.rest import AssetPairInfo

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
    """Return ``(rules, wire_pairs)`` keyed by hummingbot pair; wire pair is the ``altname``."""
    rules: dict[str, TradingRule] = {}
    wire_pairs: dict[str, str] = {}
    for info in asset_pairs.values():
        hb_pair = hb_pair_from_wsname(info.wsname)
        if hb_pair is None or not info.altname:
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


def quantize_amount(rule: TradingRule | None, amount: Decimal) -> Decimal:
    """Round ``amount`` down to the lot size; 0 when unknown or below the minimum order size."""
    if rule is None:
        return Decimal(0)
    quantized = amount.quantize(Decimal(1).scaleb(-rule.amount_decimals), rounding=ROUND_DOWN)
    return quantized if quantized >= rule.min_order_size else Decimal(0)


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
