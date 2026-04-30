"""KrakenConnectorBridge — hummingbot ConnectorBase adapter for KrakenGateway.

This module provides the thin adapter between the hummingbot connector interface
and :class:`~market_connector.exchanges.kraken.kraken_gateway.KrakenGateway`.

Design choices:
    - ``ConnectorBase`` is NOT imported here.  hb-market-connector is usable
      as a standalone library; importing hummingbot would make it a hard
      dependency.  The bridge is a structurally equivalent placeholder that
      can be subclassed when used within the hummingbot process.
    - Stale-order reconciliation is called from :meth:`start`, NOT from
      ``__init__``, so that object construction is always safe.
    - ``place_order`` delegates order-type translation to
      :func:`~..converters.kraken_ordertype_from_hb`, which is the
      integration point for ``_for_bleed/kraken-new-order-types``.
    - Tier configuration defaults to ``STARTER``; callers using ``INTERMEDIATE``
      or ``PRO`` accounts should pass the appropriate
      :class:`~..schemas.enums.KrakenAPITier` value.

Usage::

    bridge = KrakenConnectorBridge(api_key="...", secret_key="...", tier=KrakenAPITier.PRO)
    await bridge.start()
    balance = await bridge.get_balance("XXBT")
    txid = await bridge.place_order(
        trading_pair="XBTUSD",
        order_type="LIMIT",
        side="buy",
        amount=Decimal("0.001"),
        price=Decimal("50000"),
    )
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from market_connector.exchanges.kraken.converters import kraken_ordertype_from_hb
from market_connector.exchanges.kraken.hb_compat.kraken_startup_cleanup import (
    reconcile_stale_orders,
)
from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway
from market_connector.exchanges.kraken.schemas.enums import KrakenAPITier

if TYPE_CHECKING:
    from decimal import Decimal

    from market_connector.primitives import OrderBookSnapshot

logger = logging.getLogger(__name__)


class KrakenConnectorBridge:
    """Thin adapter: translates hummingbot connector calls to KrakenGateway.

    This class acts as the integration point for hummingbot connectors.  It is
    intentionally *not* a subclass of ``ConnectorBase`` to keep the library
    importable without hummingbot installed.  When used inside hummingbot,
    subclass this and add ``ConnectorBase`` to the MRO.

    Args:
        api_key:    Kraken API key.
        secret_key: Kraken API secret.
        tier:       Rate-limit tier profile (default ``STARTER``).
        sandbox:    Use sandbox REST URL when ``True``.
    """

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        tier: KrakenAPITier = KrakenAPITier.STARTER,
        sandbox: bool = False,
    ) -> None:
        self._gateway = KrakenGateway(
            api_key=api_key,
            secret_key=secret_key,
            tier=tier,
            sandbox=sandbox,
        )
        self._started = False

    @property
    def gateway(self) -> KrakenGateway:
        """The underlying :class:`KrakenGateway` instance."""
        return self._gateway

    @property
    def ready(self) -> bool:
        """``True`` after :meth:`start` has completed successfully."""
        return self._started

    async def start(self) -> None:
        """Start the gateway and run startup cleanup.

        1. Starts the gateway (REST ping + WS connect).
        2. Calls :func:`reconcile_stale_orders` to filter orphaned in-flight
           orders from previous sessions.
        """
        if self._started:
            return
        await self._gateway.start()
        await reconcile_stale_orders(self._gateway, self)
        self._started = True
        logger.info("KrakenConnectorBridge started (tier=%s).", self._gateway._config.tier)

    async def stop(self) -> None:
        """Stop the gateway."""
        if not self._started:
            return
        await self._gateway.stop()
        self._started = False

    # -------------------------------------------------------------------
    # Bridge methods — delegate to gateway with type translation
    # -------------------------------------------------------------------

    async def get_balance(self, currency: str) -> Decimal:
        """Return the balance for *currency* from the gateway.

        Args:
            currency: Kraken asset code (e.g. ``"XXBT"``, ``"ZUSD"``).
        """
        return await self._gateway.get_balance(currency)

    async def get_balances(self) -> dict[str, Decimal]:
        """Return the full balance snapshot as a dict of asset code → Decimal."""
        return await self._gateway.get_balances()

    async def get_order_book(
        self,
        trading_pair: str,
        depth: int = 25,
    ) -> OrderBookSnapshot:
        """Return an order-book snapshot for *trading_pair*.

        Args:
            trading_pair: Hummingbot canonical pair (``BTC-USD``) or
                exchange-native pair (``XBTUSD``).
            depth: Order-book depth (default ``25``).
        """
        return await self._gateway.get_orderbook(trading_pair, depth=depth)

    async def place_order(
        self,
        trading_pair: str,
        order_type: Any,
        side: Any,
        amount: Decimal,
        price: Decimal | None = None,
    ) -> str:
        """Place an order via the gateway with order-type translation.

        *order_type* is translated to a Kraken-native string via
        :func:`~..converters.kraken_ordertype_from_hb`.  This is the
        integration point for ``_for_bleed/kraken-new-order-types``.

        Args:
            trading_pair: Exchange-native pair string (e.g. ``"XBTUSD"``).
            order_type:   ``OrderType`` enum, conditional type, or string.
            side:         ``TradeType`` enum or string (``"buy"`` / ``"sell"``).
            amount:       Order volume as a ``Decimal``.
            price:        Limit price; required for limit orders.

        Returns:
            The first txid string from the Kraken API response.
        """
        kraken_ot = kraken_ordertype_from_hb(order_type)
        return await self._gateway.place_order(
            trading_pair=trading_pair,
            order_type=kraken_ot,
            side=side,
            amount=amount,
            price=price,
        )

    async def cancel_order(self, trading_pair: str, txid: str) -> bool:
        """Cancel an order by txid.

        Args:
            trading_pair: Not used by Kraken cancel; present for interface parity.
            txid:         The Kraken transaction ID to cancel.

        Returns:
            ``True`` if the order was cancelled.
        """
        return await self._gateway.cancel_order(trading_pair=trading_pair, txid=txid)


__all__ = [
    "KrakenConnectorBridge",
]
