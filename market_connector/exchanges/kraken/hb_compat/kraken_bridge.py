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
    - ``place_order`` passes the order type straight to the gateway, which maps
      it by enum name (conditional orders supported, trailing deferred; #209).
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

from typing import TYPE_CHECKING, Any

from market_connector.exchanges.kraken.hb_compat.kraken_startup_cleanup import (
    reconcile_stale_orders,
)
from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway
from market_connector.exchanges.kraken.mixins.orders import (
    is_triggered_order_type,
    warn_legacy_trigger,
)
from market_connector.exchanges.kraken.schemas.enums import KrakenAPITier
from market_connector.hb_compat.logging import get_logger

if TYPE_CHECKING:
    from decimal import Decimal

    from data_type_primitives import TriggerSpec

    from market_connector.primitives import OrderBookSnapshot

logger = get_logger(__name__)


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
        sandbox:    Unsupported: ``True`` raises ``ValueError`` (no Kraken spot sandbox, #260).
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
        *,
        limit_price: Decimal | None = None,
        price_in_percent: bool = False,
        userref: int | None = None,
        trigger: TriggerSpec | None = None,
    ) -> str:
        """Place an order via the gateway.

        *order_type* is passed through unchanged; the gateway maps it by name
        (hummingbot, local mirror, or string), so conditional types
        (STOP_LOSS, TAKE_PROFIT and their ``*_LIMIT`` variants) work end to
        end, as do TRAILING_STOP / TRAILING_STOP_LIMIT (#223).

        Args:
            trading_pair: Exchange-native pair string (e.g. ``"XBTUSD"``).
            order_type:   ``OrderType`` enum, conditional type, or string.
            side:         ``TradeType`` enum or string (``"buy"`` / ``"sell"``).
            amount:       Order volume as a ``Decimal``.
            price:        Limit price, or the trigger for conditional orders.
            limit_price:  Limit price (Kraken ``price2``) for ``*_LIMIT`` types.
            price_in_percent: Send the trigger as a percentage offset.
            userref:      Optional int32 tag for finding the order after a lost response.
            trigger:      Venue-neutral trigger, forwarded with the gateway's validation and
                          argument convention (see ``KrakenGateway.place_order``).

        Returns:
            The first txid string from the Kraken API response.

        Warns:
            DeprecationWarning: A triggered type placed without ``trigger`` (removal in #241).
        """
        extra: dict[str, Any] = {} if userref is None else {"userref": userref}
        if trigger is not None:
            extra["trigger"] = trigger
        # The warning is emitted here so it points at the bridge's caller, not the bridge;
        # the triggered path then uses the non-warning gateway entry point.
        warn_legacy_trigger(order_type, trigger, stacklevel=2)
        place = (
            self._gateway._place_order_compat
            if trigger is not None or is_triggered_order_type(order_type)
            else self._gateway.place_order
        )
        return await place(
            trading_pair=trading_pair,
            order_type=order_type,
            side=side,
            amount=amount,
            price=price,
            limit_price=limit_price,
            price_in_percent=price_in_percent,
            **extra,
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
