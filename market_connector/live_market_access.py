"""LiveMarketAccess — ConnectorBase adapter for MarketAccessProtocol + TradingRulesProtocol.

This module provides a thin wrapper around hummingbot's ConnectorBase that satisfies
the strategy-framework protocols without importing hummingbot directly (optional dep).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any


class LiveMarketAccess:
    """Wraps a hummingbot ConnectorBase to satisfy MarketAccessProtocol + TradingRulesProtocol.

    The connector is typed as ``Any`` to avoid importing ConnectorBase directly —
    hummingbot is an optional runtime dependency. The trading pair is bound at
    construction so all protocol methods are zero-argument (or currency-only).

    Usage::

        connector = ...  # a hummingbot ConnectorBase instance
        market = LiveMarketAccess(connector, trading_pair="BTC-USDT")

        order_id = market.place_order("LIMIT", "BUY", Decimal("0.001"), Decimal("50000"))
        market.cancel_order(order_id)
        mid = market.get_mid_price()
        balance = market.get_available_balance("USDT")
        rules = market.get_trading_rules("BTC-USDT")
    """

    def __init__(self, connector: Any, trading_pair: str) -> None:
        """Initialise the adapter.

        Args:
            connector: A hummingbot ``ConnectorBase`` instance.
            trading_pair: The trading pair this adapter is bound to (e.g. ``"BTC-USDT"``).
        """
        self._connector = connector
        self._trading_pair = trading_pair

    # ------------------------------------------------------------------
    # MarketAccessProtocol
    # ------------------------------------------------------------------

    def place_order(
        self,
        order_type: str,
        side: str,
        amount: Decimal,
        price: Decimal,
    ) -> str:
        """Place a limit or market order; return the client order ID.

        Args:
            order_type: ``"LIMIT"`` or ``"MARKET"`` (case-insensitive).
            side: ``"BUY"`` or ``"SELL"`` (case-insensitive).
            amount: Order quantity in base asset.
            price: Order price in quote asset.

        Returns:
            The client order ID assigned by the connector.
        """
        from hummingbot.core.data_type.common import OrderType, TradeType

        ot = OrderType[order_type.upper()]
        tt = TradeType[side.upper()]

        if tt == TradeType.BUY:
            return self._connector.buy(self._trading_pair, amount, ot, price)
        return self._connector.sell(self._trading_pair, amount, ot, price)

    def cancel_order(self, order_id: str) -> None:
        """Cancel an open order by its client order ID.

        Args:
            order_id: The client order ID to cancel.
        """
        self._connector.cancel(self._trading_pair, order_id)

    def get_mid_price(self) -> Decimal:
        """Return the current mid price for the bound trading pair.

        Returns:
            Mid price as a ``Decimal``.
        """
        return self._connector.get_mid_price(self._trading_pair)

    def get_available_balance(self, currency: str) -> Decimal:
        """Return the available (unlocked) balance for a currency.

        Args:
            currency: Asset symbol, e.g. ``"USDT"`` or ``"BTC"``.

        Returns:
            Available balance as a ``Decimal``.
        """
        return self._connector.get_available_balance(currency)

    # ------------------------------------------------------------------
    # TradingRulesProtocol
    # ------------------------------------------------------------------

    def get_trading_rules(self, trading_pair: str) -> Any:
        """Return strategy-framework ``TradingRules`` for the given pair.

        Converts the hummingbot ``TradingRule`` stored on the connector's
        ``trading_rules`` dict into the framework's immutable ``TradingRules``
        primitive.

        Args:
            trading_pair: The pair to look up (e.g. ``"BTC-USDT"``).

        Returns:
            A ``strategy_framework.primitives.trading_rules.TradingRules`` instance.

        Raises:
            KeyError: If no trading rules exist for the requested pair.
        """
        from strategy_framework.primitives.trading_rules import TradingRules

        hb_rule = self._connector.trading_rules[trading_pair]
        return TradingRules(
            trading_pair=hb_rule.trading_pair,
            min_order_size=Decimal(str(hb_rule.min_order_size)),
            max_order_size=Decimal(str(hb_rule.max_order_size))
            if hb_rule.max_order_size is not None
            else None,
            min_price_increment=Decimal(str(hb_rule.min_price_increment)),
            min_base_amount_increment=Decimal(str(hb_rule.min_base_amount_increment)),
            min_notional_size=Decimal(str(hb_rule.min_notional_size)),
            supports_limit_orders=bool(hb_rule.supports_limit_orders),
            supports_market_orders=bool(hb_rule.supports_market_orders),
        )

    def quantize_order_amount(self, trading_pair: str, amount: Decimal) -> Decimal:
        """Quantize an order amount to the exchange's minimum increment.

        Args:
            trading_pair: The pair the order will be placed on.
            amount: Raw order amount.

        Returns:
            Quantized amount as a ``Decimal``.
        """
        return self._connector.quantize_order_amount(trading_pair, amount)

    def quantize_order_price(self, trading_pair: str, price: Decimal) -> Decimal:
        """Quantize an order price to the exchange's minimum increment.

        Args:
            trading_pair: The pair the order will be placed on.
            price: Raw order price.

        Returns:
            Quantized price as a ``Decimal``.
        """
        return self._connector.quantize_order_price(trading_pair, price)
