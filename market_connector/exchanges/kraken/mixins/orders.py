"""OrdersMixin: order placement, cancellation, and open-order queries via Kraken REST API."""

from __future__ import annotations

import math
import time
import uuid
from typing import TYPE_CHECKING, Any

import httpx
from data_type_primitives import TriggerKind, TriggerReference
from data_type_primitives.common import OrderType as CanonicalOrderType

from market_connector.capabilities import OrderCapabilities
from market_connector.exceptions import (
    ExchangeUnavailableError,
    GatewayNotStartedError,
    OrderOutcomeUnknownError,
    UnsupportedOrderError,
)
from market_connector.exchanges.kraken.age_penalties import (
    amend_penalty,
    cancel_penalty,
    edit_penalty,
)
from market_connector.exchanges.kraken.errors import OpenOrdersLimitError, raise_on_kraken_error
from market_connector.exchanges.kraken.pairs import resolve_wire_pair
from market_connector.exchanges.kraken.schemas.rest import (
    AddOrderBatchResult,
    AddOrderResult,
    AmendOrderResult,
    CancelOrderResult,
    EditOrderResult,
    KrakenResponse,
    OrderInfo,
    TradeInfo,
)
from market_connector.exchanges.kraken.specs import (
    KRAKEN_MAX_OPEN_ORDERS_PER_PAIR,
    KRAKEN_RATE_LIMIT_SPEC,
)
from market_connector.rate_limits.limiter import ensure_keyed_limit

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from decimal import Decimal

    from market_connector.exchanges.kraken.mixins.protocols import HasReady, HasRest
    from market_connector.primitives import OrderType, TradeType
    from market_connector.transport.response import Response
    from market_connector.transport.rest_base import RestConnectorBase

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_ORDER_TYPE_MAP: dict[str, str] = {
    "MARKET": "market",
    "LIMIT": "limit",
    "LIMIT_MAKER": "limit",  # sent with oflags="post" (post-only)
    "STOP_LOSS": "stop-loss",
    "TAKE_PROFIT": "take-profit",
    "STOP_LOSS_LIMIT": "stop-loss-limit",
    "TAKE_PROFIT_LIMIT": "take-profit-limit",
    "TRAILING_STOP": "trailing-stop",
    "TRAILING_STOP_LIMIT": "trailing-stop-limit",
}

# Trailing types: ``price`` is the trail distance, ``price2`` (limit) a signed offset.
_TRAILING_KEYS = frozenset({"TRAILING_STOP", "TRAILING_STOP_LIMIT"})
# Conditional types whose ``price`` is the trigger.
_CONDITIONAL_KEYS = frozenset(_ORDER_TYPE_MAP) - {"MARKET", "LIMIT", "LIMIT_MAKER"} - _TRAILING_KEYS
# Conditional types that also carry a limit price (sent as ``price2``).
_LIMIT_KEYS = frozenset({"STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT"})
# Kraken cl_ord_id: ASCII up to this many characters, or a UUID.
_CL_ORD_ID_MAX_LEN = 18

# Derived from _ORDER_TYPE_MAP so the descriptor cannot drift from the adapter.
# Triggers: absolute price, percent (``price_in_percent``) and trailing distance offsets.
KRAKEN_ORDER_CAPABILITIES = OrderCapabilities(
    order_types=frozenset(CanonicalOrderType[name] for name in _ORDER_TYPE_MAP),
    # RELATIVE_OFFSET is not advertised until Kraken adopts TriggerSpec (P6, #240).
    trigger_kinds=frozenset({TriggerKind.ABSOLUTE, TriggerKind.PERCENT}),
    trigger_references=frozenset({TriggerReference.LAST}),
    supports_trailing=True,
    linked=frozenset(),
    native_linked=False,
)


def _order_type_key(order_type: object) -> str:
    """Name-based key for any OrderType enum (hummingbot or local) or string."""
    name = getattr(order_type, "name", None)
    return name.upper() if isinstance(name, str) else str(order_type).upper().replace("-", "_")


def _to_kraken_ordertype(order_type: object) -> str:
    """Translate an OrderType (any enum with a matching ``name``) or string to Kraken's.

    Args:
        order_type: An OrderType member from ``data_type_primitives`` or the local
            mirror (matched by name), or a case-insensitive string.

    Raises:
        UnsupportedOrderError: A valid canonical type Kraken does not map (e.g. ``AMM_SWAP``).
        ValueError: For names that are not a canonical order type (malformed input).
    """
    key = _order_type_key(order_type)
    if key in _ORDER_TYPE_MAP:
        return _ORDER_TYPE_MAP[key]
    if key in CanonicalOrderType.__members__:
        raise UnsupportedOrderError(
            exchange="kraken", order_type=key, reason="order type not supported by Kraken"
        )
    raise ValueError(f"Unsupported order type: {order_type!r}.")


def _trailing_fields(
    key: str,
    price: Decimal | None,
    limit_price: Decimal | None,
    price_in_percent: bool,
) -> dict[str, object]:
    """Validate trailing arguments and return the relative ``price``/``price2`` fields."""
    if price is None or price <= 0:
        raise ValueError(f"price (trail distance) must be > 0 for {key} orders")
    if key == "TRAILING_STOP" and limit_price is not None:
        raise ValueError("limit_price is not valid for TRAILING_STOP orders")
    if key == "TRAILING_STOP_LIMIT" and limit_price is None:
        raise ValueError("limit_price (offset from trigger) is required for TRAILING_STOP_LIMIT")
    suffix = "%" if price_in_percent else ""
    fields: dict[str, object] = {"price": f"+{price:f}{suffix}"}
    if limit_price is not None:
        sign = "-" if limit_price < 0 else "+"
        fields["price2"] = f"{sign}{abs(limit_price):f}{suffix}"
    return fields


def _build_price_fields(
    key: str,
    price: Decimal | None,
    limit_price: Decimal | None,
    price_in_percent: bool,
) -> dict[str, object]:
    """Validate price arguments for ``key`` and return the ``price``/``price2`` body fields."""
    if key in _TRAILING_KEYS:
        return _trailing_fields(key, price, limit_price, price_in_percent)
    conditional = key in _CONDITIONAL_KEYS
    if key in {"LIMIT", "LIMIT_MAKER"} and price is None:
        raise ValueError(f"price is required for {key} orders")
    if conditional and price is None:
        raise ValueError(f"price (trigger) is required for {key} orders")
    if key in _LIMIT_KEYS and limit_price is None:
        raise ValueError(f"limit_price is required for {key} orders")
    if limit_price is not None and key not in _LIMIT_KEYS:
        raise ValueError(f"limit_price is only valid for *_LIMIT conditional orders, not {key}")
    if price_in_percent and not conditional:
        raise ValueError(f"price_in_percent is only valid for conditional orders, not {key}")

    fields: dict[str, object] = {}
    if price is not None:
        fields["price"] = f"#{price}%" if price_in_percent else str(price)
    if limit_price is not None:
        fields["price2"] = str(limit_price)
    return fields


def _to_kraken_side(side: TradeType | str) -> str:
    """Translate a TradeType enum or string to a Kraken side string.

    Kraken expects ``"buy"`` or ``"sell"`` (lowercase).
    """
    name = getattr(side, "name", None)
    return name.lower() if isinstance(name, str) else str(side).lower()


async def _request_keyed(
    rest: RestConnectorBase,
    endpoint_name: str,
    pair: str,
    body: dict[str, object],
    matching_units: int | None = None,
) -> Response:
    """Send ``endpoint_name`` charged to the per-pair matching limit when supported.

    ``matching_units`` overrides the units charged to the pair's matching counter; 0
    skips that counter but the declared per-pair endpoint limit is still reserved.
    REST clients without a ``rate_limiter`` (e.g. test doubles) are called unkeyed.
    """
    limiter = getattr(rest, "rate_limiter", None)
    if limiter is None:
        return await rest.request(endpoint_name, data=body)
    limit_id = ensure_keyed_limit(
        limiter, endpoint_name, pair, KRAKEN_RATE_LIMIT_SPEC.keyed_endpoint_pools[endpoint_name]
    )
    pools = None if matching_units is None else {f"matching:{pair}": matching_units}
    return await rest.request(endpoint_name, data=body, limit_id=limit_id, pools=pools)


async def _request_multi_keyed(
    rest: RestConnectorBase,
    endpoint_name: str,
    matching_units: dict[str, int],
    body: dict[str, object],
) -> Response:
    """Send ``endpoint_name`` charging ``matching_units`` (pair -> units) to each pair's pool.

    The first pair's declared endpoint limit is reserved; every pair's matching pool is
    registered first and charged through an ad-hoc multi-pool reserve (0 skips a pool).
    """
    limiter = getattr(rest, "rate_limiter", None)
    if limiter is None:
        return await rest.request(endpoint_name, data=body)
    charges = KRAKEN_RATE_LIMIT_SPEC.keyed_endpoint_pools[endpoint_name]
    limit_ids = [
        ensure_keyed_limit(limiter, endpoint_name, pair, charges) for pair in matching_units
    ]
    pools = {f"matching:{pair}": units for pair, units in matching_units.items()}
    return await rest.request(endpoint_name, data=body, limit_id=limit_ids[0], pools=pools)


def _validate_cl_ord_id(cl_ord_id: str) -> None:
    """Raise ``ValueError`` unless ``cl_ord_id`` is ASCII up to 18 characters or a UUID."""
    if cl_ord_id.isascii() and 1 <= len(cl_ord_id) <= _CL_ORD_ID_MAX_LEN:
        return
    try:
        is_uuid = cl_ord_id.isascii() and len(cl_ord_id) in {32, 36} and bool(uuid.UUID(cl_ord_id))
    except ValueError:
        is_uuid = False
    if not is_uuid:
        raise ValueError(
            f"Invalid cl_ord_id {cl_ord_id!r}: must be ASCII up to {_CL_ORD_ID_MAX_LEN} "
            "characters or a UUID"
        )


def _order_filter(userref: int | None, cl_ord_id: str | None) -> dict[str, object]:
    """Build the OpenOrders/ClosedOrders filter; both identifiers set is a ``ValueError``."""
    if userref is not None and cl_ord_id is not None:
        raise ValueError("userref and cl_ord_id are mutually exclusive")
    if userref is not None:
        return {"userref": userref}
    if cl_ord_id is not None:
        return {"cl_ord_id": cl_ord_id}
    return {}


async def _orders_by_filter(
    rest: RestConnectorBase, endpoint_name: str, filters: dict[str, object]
) -> dict[str, Any]:
    """POST ``endpoint_name`` (with ``filters`` when non-empty); return the checked raw reply."""
    if not filters:
        response = await rest.request(endpoint_name)
    else:
        response = await rest.request(endpoint_name, data=filters)
    raw: dict[str, Any] = response.raw  # type: ignore[assignment]
    raise_on_kraken_error(raw.get("error", []))
    return raw


def _placed_at(owner: object) -> dict[str, float]:
    """The owner's txid -> placement-time map, created on first use (mixins are stateless)."""
    return vars(owner).setdefault("_order_placed_at", {})  # type: ignore[no-any-return]


def _age(owner: OrdersMixin, txid: str) -> float:
    """Seconds since this process placed ``txid``; 0 (worst-case penalty) if unknown."""
    placed = _placed_at(owner).get(txid)
    return 0.0 if placed is None else owner._order_clock() - placed


def _open_counts(owner: object) -> dict[str, int]:
    """The owner's pair -> open-order upper-bound count, created on first use."""
    return vars(owner).setdefault("_open_order_counts", {})  # type: ignore[no-any-return]


async def _ensure_room(owner: OrdersMixin, pair: str, n: int) -> None:
    """Raise ``OpenOrdersLimitError`` unless ``n`` more orders fit under the pair's cap.

    The local count only over-estimates (fills are not seen), so it is re-read from
    OpenOrders, and only when ``count + n`` would pass the cap.
    """
    counts = _open_counts(owner)
    # No tier's cap is below the smallest, so the config is only read past that.
    if counts.get(pair, 0) + n <= min(KRAKEN_MAX_OPEN_ORDERS_PER_PAIR.values()):
        return
    cap = KRAKEN_MAX_OPEN_ORDERS_PER_PAIR[owner._config.tier]  # type: ignore[attr-defined]
    if counts.get(pair, 0) + n <= cap:
        return
    open_orders = await owner.get_open_orders()
    counts[pair] = sum(1 for order in open_orders.values() if order.descr.pair == pair)
    if counts[pair] + n > cap:
        raise OpenOrdersLimitError(f"{pair} has {counts[pair]} open orders; cap is {cap}")


def _add_open(owner: object, pair: str, delta: int) -> None:
    """Move the pair's open-order count by ``delta``, never below 0."""
    counts = _open_counts(owner)
    counts[pair] = max(0, counts.get(pair, 0) + delta)


# ---------------------------------------------------------------------------
# Mixin
# ---------------------------------------------------------------------------


class OrdersMixin:
    # Monotonic seconds source for order ageing; tests inject a manual clock.
    _order_clock: Callable[[], float] = staticmethod(time.monotonic)

    async def place_order(
        self: HasRest & HasReady,  # type: ignore[valid-type]
        trading_pair: str,
        order_type: OrderType | str,
        side: TradeType | str,
        amount: Decimal,
        price: Decimal | None = None,
        *,
        limit_price: Decimal | None = None,
        price_in_percent: bool = False,
        userref: int | None = None,
        cl_ord_id: str | None = None,
    ) -> str:
        """Place an order on Kraken and return the first txid.

        Args:
            trading_pair: Exchange-native pair (``"XBTUSD"``, passed through) or a
                hb pair (``"BTC-USD"``, converted to the AssetPairs ``altname``).
            order_type: MARKET, LIMIT, STOP_LOSS, TAKE_PROFIT, STOP_LOSS_LIMIT,
                TAKE_PROFIT_LIMIT, TRAILING_STOP or TRAILING_STOP_LIMIT, as an enum
                (matched by name) or string.
            side: ``TradeType.BUY`` / ``TradeType.SELL`` or lowercase strings.
            amount: Order volume as a ``Decimal``.
            price: Limit price for LIMIT; the trigger (Kraken ``price``) for
                conditional types.
            limit_price: Kraken ``price2``; required for ``*_LIMIT`` types only. For
                TRAILING_STOP_LIMIT it is a signed offset from the trigger.
            price_in_percent: Send the trigger as ``#<price>%`` (conditional), or the
                trail distance/offset as ``+<d>%`` (trailing).
            userref: Optional int32 client tag (Kraken ``userref``); lets the order be
                found again via ``get_open_orders`` / ``get_closed_orders`` when the
                AddOrder response is lost.
            cl_ord_id: Optional client order id (Kraken ``cl_ord_id``): ASCII up to
                18 characters or a UUID, unique among OPEN orders, mutually exclusive
                with ``userref``. Used by linked-order emulation with
                ``KrakenClOrdIdLinkCodec`` (#248, #255).

        Returns:
            The first txid string from ``result.txid``.

        Raises:
            GatewayNotStartedError: If the gateway is not ready.
            ValueError: For an unknown dashed pair, unsupported order types,
                inconsistent price arguments, both ``userref`` and ``cl_ord_id``, or an
                invalid ``cl_ord_id``; raised before any request.
            OrderOutcomeUnknownError: Only when ``cl_ord_id`` is set and the reply was
                lost (timeout or transport error). The outcome is unknown; resolve it
                with ``get_open_orders`` / ``get_closed_orders(cl_ord_id=...)``. Any
                other exception means the order was definitely not placed.
        """
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")
        if cl_ord_id is not None:
            if userref is not None:
                raise ValueError("userref and cl_ord_id are mutually exclusive")
            _validate_cl_ord_id(cl_ord_id)

        kraken_ordertype = _to_kraken_ordertype(order_type)
        key = _order_type_key(order_type)
        price_fields = _build_price_fields(key, price, limit_price, price_in_percent)
        if key == "LIMIT_MAKER":
            price_fields["oflags"] = "post"
        trading_pair = await resolve_wire_pair(self, trading_pair)

        body: dict[str, object] = {
            "pair": trading_pair,
            "type": _to_kraken_side(side),
            "ordertype": kraken_ordertype,
            "volume": str(amount),
            **price_fields,
        }
        if userref is not None:
            body["userref"] = userref
        if cl_ord_id is not None:
            body["cl_ord_id"] = cl_ord_id

        await _ensure_room(self, trading_pair, 1)
        try:
            response = await _request_keyed(self._rest, "add_order", trading_pair, body)
        except OrderOutcomeUnknownError:
            raise
        except (httpx.TimeoutException, httpx.TransportError, ExchangeUnavailableError) as exc:
            if cl_ord_id is None:
                raise
            raise OrderOutcomeUnknownError(
                f"AddOrder reply lost for cl_ord_id={cl_ord_id!r}: {exc}",
                client_order_id=cl_ord_id,
            ) from exc
        # Error replies carry no result, so raise before validating it.
        raise_on_kraken_error(response.raw.get("error", []))  # type: ignore[union-attr]
        envelope = KrakenResponse[AddOrderResult].model_validate(response.raw)
        raise_on_kraken_error(envelope.error)
        placed = self._order_clock()
        for txid in envelope.result.txid:  # type: ignore[union-attr]
            _placed_at(self)[txid] = placed
        _add_open(self, trading_pair, len(envelope.result.txid))  # type: ignore[union-attr]
        return envelope.result.txid[0]  # type: ignore[union-attr]

    async def cancel_order(
        self: HasRest & HasReady,  # type: ignore[valid-type]
        trading_pair: str,
        txid: str | None = None,
        *,
        cl_ord_id: str | None = None,
    ) -> bool:
        """Cancel an order by txid or by ``cl_ord_id`` (exactly one).

        Args:
            trading_pair: Not sent to Kraken's cancel endpoint; keys the per-pair
                matching rate limit.
            txid: The Kraken transaction ID to cancel.
            cl_ord_id: The client order id the order was placed with.

        Returns:
            ``True`` if ``result.count >= 1`` (order was cancelled).

        Raises:
            GatewayNotStartedError: If the gateway is not ready.
            ValueError: Unless exactly one of ``txid`` / ``cl_ord_id`` is given;
                raised before any request.

        The matching counter is charged the order-age penalty. The age comes from
        this process's ``place_order``; an unknown or ``cl_ord_id`` order is charged
        the worst case.
        """
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")
        if (txid is None) == (cl_ord_id is None):
            raise ValueError("Exactly one of txid or cl_ord_id is required")

        placed = None if txid is None else _placed_at(self).get(txid)
        units = cancel_penalty(0.0 if placed is None else self._order_clock() - placed)
        body: dict[str, object] = {"txid": txid} if txid is not None else {"cl_ord_id": cl_ord_id}
        response = await _request_keyed(self._rest, "cancel_order", trading_pair, body, units)
        envelope = KrakenResponse[CancelOrderResult].model_validate(response.raw)
        raise_on_kraken_error(envelope.error)
        if txid is not None:
            _placed_at(self).pop(txid, None)
        _add_open(self, trading_pair, -1)
        return envelope.result.count >= 1  # type: ignore[union-attr]

    async def amend_order(
        self: HasRest & HasReady,  # type: ignore[valid-type]
        trading_pair: str,
        txid: str,
        *,
        amount: Decimal | None = None,
        price: Decimal | None = None,
    ) -> str:
        """Amend an order in place (keeps its txid) and return the ``amend_id``.

        Unset ``amount`` / ``price`` are omitted from the request. The matching
        counter is charged the amend penalty for the order's age, which then restarts.

        Raises:
            GatewayNotStartedError: If the gateway is not ready.
        """
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")

        body: dict[str, object] = {"txid": txid}
        if amount is not None:
            body["order_qty"] = str(amount)
        if price is not None:
            body["limit_price"] = str(price)
        units = amend_penalty(_age(self, txid))
        response = await _request_keyed(self._rest, "amend_order", trading_pair, body, units)
        envelope = KrakenResponse[AmendOrderResult].model_validate(response.raw)
        raise_on_kraken_error(envelope.error)
        _placed_at(self)[txid] = self._order_clock()
        return envelope.result.amend_id  # type: ignore[union-attr]

    async def edit_order(
        self: HasRest & HasReady,  # type: ignore[valid-type]
        trading_pair: str,
        txid: str,
        *,
        amount: Decimal | None = None,
        price: Decimal | None = None,
    ) -> str:
        """Edit an order (cancel and replace) and return the new txid.

        Kraken EditOrder does not support ``cl_ord_id`` and assigns a new txid, so
        linked-order legs must use ``amend_order`` or cancel/replace, never this.

        Unset ``amount`` / ``price`` are omitted from the request. The matching
        counter is charged the edit penalty for the order's age; the old txid is
        forgotten and the new one starts ageing now.

        Raises:
            GatewayNotStartedError: If the gateway is not ready.
        """
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")

        body: dict[str, object] = {"txid": txid, "pair": trading_pair}
        if amount is not None:
            body["volume"] = str(amount)
        if price is not None:
            body["price"] = str(price)
        units = edit_penalty(_age(self, txid))
        response = await _request_keyed(self._rest, "edit_order", trading_pair, body, units)
        envelope = KrakenResponse[EditOrderResult].model_validate(response.raw)
        raise_on_kraken_error(envelope.error)
        _placed_at(self).pop(txid, None)
        new_txid = envelope.result.txid  # type: ignore[union-attr]
        _placed_at(self)[new_txid] = self._order_clock()
        return new_txid

    async def place_orders_batch(
        self: HasRest & HasReady,  # type: ignore[valid-type]
        trading_pair: str,
        orders: Sequence[dict[str, object]],
    ) -> list[str]:
        """Place 2 to 15 orders on one pair in a single request; return their txids.

        Args:
            trading_pair: Exchange-native pair shared by every order.
            orders: Kraken-native order dicts (``type``, ``ordertype``, ``volume``,
                optional ``price``), sent as given.

        The matching counter is charged ``ceil(len(orders) / 2)``.

        Raises:
            GatewayNotStartedError: If the gateway is not ready.
            ValueError: If there are not 2 to 15 orders (nothing is sent).
        """
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")
        if not 2 <= len(orders) <= 15:
            raise ValueError(f"A batch takes 2 to 15 orders, got {len(orders)}")

        body: dict[str, object] = {"pair": trading_pair, "orders": list(orders)}
        await _ensure_room(self, trading_pair, len(orders))
        response = await _request_keyed(
            self._rest, "add_order_batch", trading_pair, body, math.ceil(len(orders) / 2)
        )
        envelope = KrakenResponse[AddOrderBatchResult].model_validate(response.raw)
        raise_on_kraken_error(envelope.error)
        placed = self._order_clock()
        txids = [entry.txid for entry in envelope.result.orders]  # type: ignore[union-attr]
        for txid in txids:
            _placed_at(self)[txid] = placed
        _add_open(self, trading_pair, len(txids))
        return txids

    async def cancel_orders_batch(
        self: HasRest & HasReady,  # type: ignore[valid-type]
        orders: Sequence[tuple[str, str]],
    ) -> int:
        """Cancel up to 50 ``(trading_pair, txid)`` orders in a single request.

        Returns:
            The number of orders cancelled (``result.count``).

        Each pair's matching counter is charged the sum of its orders' cancel
        penalties; a pair whose sum is 0 is not charged (the endpoint limit still is).

        Raises:
            GatewayNotStartedError: If the gateway is not ready.
            ValueError: If there are not 1 to 50 orders (nothing is sent).
        """
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")
        if not 1 <= len(orders) <= 50:
            raise ValueError(f"A cancel batch takes 1 to 50 orders, got {len(orders)}")

        units: dict[str, int] = {}
        for pair, txid in orders:
            units[pair] = units.get(pair, 0) + cancel_penalty(_age(self, txid))
        body: dict[str, object] = {"orders": [txid for _, txid in orders]}
        response = await _request_multi_keyed(self._rest, "cancel_order_batch", units, body)
        envelope = KrakenResponse[CancelOrderResult].model_validate(response.raw)
        raise_on_kraken_error(envelope.error)
        for pair, txid in orders:
            _placed_at(self).pop(txid, None)
            _add_open(self, pair, -1)
        return envelope.result.count  # type: ignore[union-attr]

    async def get_open_orders(
        self: HasRest & HasReady,  # type: ignore[valid-type]
        userref: int | None = None,
        *,
        cl_ord_id: str | None = None,
    ) -> dict[str, OrderInfo]:
        """Return open orders keyed by txid, optionally filtered by ``userref`` or ``cl_ord_id``.

        Kraken's ``/0/private/OpenOrders`` response wraps the order map under
        the ``"open"`` key: ``{"result": {"open": {<txid>: {...}}}}``.  This
        method unwraps that layer and validates each value as ``OrderInfo``.

        Raises:
            GatewayNotStartedError: If the gateway is not ready.
            ValueError: If both ``userref`` and ``cl_ord_id`` are set (nothing is sent).
        """
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")

        raw = await _orders_by_filter(self._rest, "open_orders", _order_filter(userref, cl_ord_id))
        open_dict: dict[str, object] = raw.get("result", {}).get("open", {})
        return {txid: OrderInfo.model_validate(info) for txid, info in open_dict.items()}

    async def get_closed_orders(
        self: HasRest & HasReady,  # type: ignore[valid-type]
        userref: int | None = None,
        *,
        cl_ord_id: str | None = None,
    ) -> dict[str, OrderInfo]:
        """Return closed orders keyed by txid, optionally filtered by ``userref`` or ``cl_ord_id``.

        Unwraps ``result.closed`` of ``/0/private/ClosedOrders``. Only the first page
        is returned.

        Raises:
            GatewayNotStartedError: If the gateway is not ready.
            ValueError: If both ``userref`` and ``cl_ord_id`` are set (nothing is sent).
        """
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")

        raw = await _orders_by_filter(
            self._rest, "closed_orders", _order_filter(userref, cl_ord_id)
        )
        closed_dict: dict[str, object] = raw.get("result", {}).get("closed", {})
        return {txid: OrderInfo.model_validate(info) for txid, info in closed_dict.items()}

    async def query_orders(
        self: HasRest & HasReady,  # type: ignore[valid-type]
        txids: list[str],
    ) -> dict[str, OrderInfo]:
        """Batch-query orders by txid list.

        Args:
            txids: List of Kraken transaction IDs to look up.

        Returns:
            Dict mapping txid → ``OrderInfo``.

        Raises:
            GatewayNotStartedError: If the gateway is not ready.
        """
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")

        response = await self._rest.request("query_orders", data={"txid": ",".join(txids)})
        raw = response.raw
        raise_on_kraken_error(raw.get("error", []))
        result_dict: dict[str, object] = raw.get("result", {})
        return {txid: OrderInfo.model_validate(info) for txid, info in result_dict.items()}

    async def query_trades(
        self: HasRest & HasReady,  # type: ignore[valid-type]
        txids: list[str],
    ) -> dict[str, TradeInfo]:
        """Batch-query trades by txid list.

        Args:
            txids: List of Kraken trade IDs to look up.

        Returns:
            Dict mapping txid → ``TradeInfo``.

        Raises:
            GatewayNotStartedError: If the gateway is not ready.
        """
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")

        response = await self._rest.request("query_trades", data={"txid": ",".join(txids)})
        raw = response.raw
        raise_on_kraken_error(raw.get("error", []))
        result_dict: dict[str, object] = raw.get("result", {})
        return {txid: TradeInfo.model_validate(info) for txid, info in result_dict.items()}
