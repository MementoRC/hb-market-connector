"""Kraken pair-name conversion: hb pair (``BTC-USD``) -> AssetPairs ``altname`` (``XBTUSD``).

Pure helpers plus the gateway's lazy, cached pair map. Kept free of hummingbot and
``hb_compat`` imports so the REST mixins can use it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from market_connector.exchanges.kraken.errors import raise_on_kraken_error
from market_connector.exchanges.kraken.schemas.rest import AssetPairInfo, KrakenResponse
from market_connector.exchanges.kraken.specs import KRAKEN_SYMBOL_MAPPER

if TYPE_CHECKING:
    from market_connector.exchanges.kraken.mixins.protocols import HasRest

logger = logging.getLogger(__name__)


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


async def resolve_wire_pair(owner: HasRest, pair: str) -> str:
    """Kraken wire pair for *pair*.

    Native pairs (no dash) pass through. A dashed hb pair is looked up in the
    ``AssetPairs`` map, fetched once per *owner* and cached on it.

    Raises:
        ValueError: If a dashed pair is not in ``AssetPairs`` (nothing is guessed).
    """
    if "-" not in pair:
        return pair
    table: dict[str, str] | None = getattr(owner, "_wire_pair_map", None)
    if table is None:
        lock: asyncio.Lock | None = getattr(owner, "_wire_pair_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            owner._wire_pair_lock = lock  # type: ignore[attr-defined]
        async with lock:
            table = getattr(owner, "_wire_pair_map", None)
            if table is None:
                response = await owner._rest.request("asset_pairs")
                envelope = KrakenResponse[dict[str, AssetPairInfo]].model_validate(response.raw)
                raise_on_kraken_error(envelope.error)
                table = build_pair_tables(envelope.result or {})[1]
                owner._wire_pair_map = table  # type: ignore[attr-defined]
    try:
        return table[pair]
    except KeyError:
        raise ValueError(f"Unknown Kraken trading pair {pair!r}: not in AssetPairs") from None
