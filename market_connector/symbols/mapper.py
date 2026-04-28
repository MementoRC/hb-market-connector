"""Symbol mapping: convert between Hummingbot canonical pairs and exchange formats.

Hummingbot canonical form: ``BASE-QUOTE`` (uppercase, dash separator).
Each exchange has its own format — this module provides lightweight, frozen
dataclass implementations that satisfy the :class:`SymbolMapper` protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

from market_connector.exceptions import UnknownPairError

__all__ = [
    "SymbolMapper",
    "IdentityMapper",
    "RuleBasedMapper",
]


class SymbolMapper(Protocol):
    """Structural protocol for bidirectional pair/asset mapping.

    Hummingbot canonical: ``BASE-QUOTE`` uppercase (e.g. ``BTC-USD``).
    """

    def to_exchange_pair(self, hb_pair: str) -> str:
        """Convert a Hummingbot canonical pair to the exchange format."""
        ...

    def from_exchange_pair(self, exch_pair: str) -> str:
        """Convert an exchange pair to Hummingbot canonical format."""
        ...

    def to_exchange_asset(self, hb_asset: str) -> str:
        """Convert a Hummingbot canonical asset to the exchange asset code."""
        ...

    def from_exchange_asset(self, exch_asset: str) -> str:
        """Convert an exchange asset code to Hummingbot canonical form."""
        ...


@dataclass(frozen=True)
class IdentityMapper:
    """Coinbase, OKX, BTC-Markets — pair format matches canonical with optional separator swap.

    If the exchange uses the same ``BASE<sep>QUOTE`` structure as Hummingbot
    (with possibly a different separator), this mapper handles the translation
    without any alias tables.
    """

    separator: str = "-"

    def to_exchange_pair(self, hb_pair: str) -> str:
        """``BTC-USD`` → ``BTC<sep>USD``."""
        parts = hb_pair.split("-")
        if len(parts) != 2:  # noqa: PLR2004
            raise UnknownPairError(hb_pair)
        return self.separator.join(parts)

    def from_exchange_pair(self, exch_pair: str) -> str:
        """``BTC<sep>USD`` → ``BTC-USD``."""
        parts = exch_pair.split(self.separator)
        if len(parts) != 2:  # noqa: PLR2004
            raise UnknownPairError(exch_pair)
        return f"{parts[0]}-{parts[1]}"

    def to_exchange_asset(self, hb_asset: str) -> str:
        """Identity — no alias table."""
        return hb_asset

    def from_exchange_asset(self, exch_asset: str) -> str:
        """Identity — no alias table."""
        return exch_asset


@dataclass(frozen=True)
class RuleBasedMapper:
    """Binance, Kraken, etc. — handles aliases and no-separator formats.

    Parameters
    ----------
    separator:
        Delimiter used by the exchange (``"/"`` for Kraken WS, ``None`` for
        concatenated pairs like ``BTCUSDT``).
    known_quote_assets:
        Ordered tuple of quote asset strings.  Required when *separator* is
        ``None``; the longest-suffix match wins (e.g. ``USDT`` beats ``USD``).
    asset_aliases_to_hb:
        Exchange asset → Hummingbot canonical asset mapping
        (e.g. ``{"XBT": "BTC", "ZUSD": "USD"}``).
    asset_aliases_from_hb:
        Hummingbot canonical asset → exchange asset mapping
        (e.g. ``{"BTC": "XBT", "USD": "ZUSD"}``).
    fallback_lookup:
        Called with the raw exchange pair string when the primary split
        strategy fails.  Should return a Hummingbot-canonical ``BASE-QUOTE``
        string or ``None`` (which triggers :class:`~market_connector.exceptions.UnknownPairError`).
    """

    separator: str | None
    known_quote_assets: tuple[str, ...] = ()
    asset_aliases_to_hb: dict[str, str] = field(default_factory=dict)
    asset_aliases_from_hb: dict[str, str] = field(default_factory=dict)
    fallback_lookup: Callable[[str], str | None] | None = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def from_exchange_pair(self, exch_pair: str) -> str:
        """Convert an exchange pair to Hummingbot canonical ``BASE-QUOTE``.

        Algorithm:
        1. If *separator* is set, split on it → ``(base_raw, quote_raw)``.
           If split yields != 2 parts, try *fallback_lookup* or raise.
        2. If *separator* is ``None``, find the longest suffix in
           *known_quote_assets* → split.  If no match, try *fallback_lookup*
           or raise.
        3. Apply *asset_aliases_to_hb* to both sides, return ``base-quote``.
        """
        base_raw, quote_raw = self._split_exchange_pair(exch_pair)
        base = self.asset_aliases_to_hb.get(base_raw, base_raw)
        quote = self.asset_aliases_to_hb.get(quote_raw, quote_raw)
        return f"{base}-{quote}"

    def to_exchange_pair(self, hb_pair: str) -> str:
        """Convert a Hummingbot canonical ``BASE-QUOTE`` to exchange format.

        Symmetric to :meth:`from_exchange_pair`:
        1. Split *hb_pair* on ``-`` → ``(base_hb, quote_hb)``.  Raise if != 2 parts.
        2. Apply *asset_aliases_from_hb* to both sides.
        3. Join with *separator* (or concatenate when ``None``).
        """
        parts = hb_pair.split("-")
        if len(parts) != 2:  # noqa: PLR2004
            raise UnknownPairError(hb_pair)
        base_hb, quote_hb = parts
        base = self.asset_aliases_from_hb.get(base_hb, base_hb)
        quote = self.asset_aliases_from_hb.get(quote_hb, quote_hb)
        if self.separator is not None:
            return f"{base}{self.separator}{quote}"
        return f"{base}{quote}"

    def to_exchange_asset(self, hb_asset: str) -> str:
        """``hb_asset`` → exchange asset code (via *asset_aliases_from_hb*)."""
        return self.asset_aliases_from_hb.get(hb_asset, hb_asset)

    def from_exchange_asset(self, exch_asset: str) -> str:
        """Exchange asset code → Hummingbot canonical (via *asset_aliases_to_hb*)."""
        return self.asset_aliases_to_hb.get(exch_asset, exch_asset)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _split_exchange_pair(self, exch_pair: str) -> tuple[str, str]:
        """Return ``(base_raw, quote_raw)`` or raise :class:`UnknownPairError`."""
        if self.separator is not None:
            parts = exch_pair.split(self.separator)
            if len(parts) == 2:  # noqa: PLR2004
                return parts[0], parts[1]
            return self._try_fallback(exch_pair)

        # No-separator mode: longest-suffix match among known_quote_assets
        best_quote: str | None = None
        for quote in self.known_quote_assets:
            if exch_pair.endswith(quote) and (best_quote is None or len(quote) > len(best_quote)):
                best_quote = quote
        if best_quote is not None:
            base = exch_pair[: -len(best_quote)]
            if base:
                return base, best_quote
        return self._try_fallback(exch_pair)

    def _try_fallback(self, exch_pair: str) -> tuple[str, str]:
        """Invoke *fallback_lookup* or raise :class:`UnknownPairError`."""
        if self.fallback_lookup is not None:
            result = self.fallback_lookup(exch_pair)
            if result is not None:
                parts = result.split("-")
                if len(parts) == 2:  # noqa: PLR2004
                    return parts[0], parts[1]
        raise UnknownPairError(exch_pair)
