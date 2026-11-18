"""LinkedOrdersMixin: Kraken bracket via native conditional close (#240).

Kraken spot has no OCO and no separate bracket legs. A bracket is ONE AddOrder for the
entry carrying a conditional close (``close[ordertype]`` / ``close[price]`` /
``close[price2]``): the exit is released by the exchange when the entry fills, so the
position stays protected if the connection is lost. There is no client-side leg
management. Kraken allows a single conditional close per order, so a bracket takes
exactly one exit (take-profit OR stop-loss).

The entry carries an int32 ``userref`` from ``KrakenUserrefLinkCodec`` (leg 0), so a lost
AddOrder reply is resolved like any order, by userref.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from data_type_primitives import (
    CancelOutcome,
    LinkedCancelOutcome,
    LinkedCancelStatus,
    LinkKind,
)

from market_connector.capabilities import unsupported_linked
from market_connector.exceptions import (
    GatewayNotStartedError,
    OrderNotFoundError,
    UnsupportedOrderError,
)
from market_connector.exchanges.kraken.linked_order_codec import KrakenUserrefLinkCodec
from market_connector.exchanges.kraken.mixins.orders import (
    _add_open,
    _order_fields,
    _send_add_order,
)
from market_connector.exchanges.kraken.pairs import resolve_wire_pair

if TYPE_CHECKING:
    from collections.abc import Mapping

    from data_type_primitives import LegSpec, LinkedOrderSpec

_CODEC = KrakenUserrefLinkCodec()
# Order types Kraken accepts as ``close[ordertype]``.
_CLOSE_ORDERTYPES = frozenset(
    {"limit", "stop-loss", "take-profit", "stop-loss-limit", "take-profit-limit"}
)
# Kraken entry status -> (entry outcome, group status) after a cancel attempt. Whether the
# group is terminal also depends on the released exit (see ``_release_exit``).
_AFTER_CANCEL: dict[str, tuple[CancelOutcome, LinkedCancelStatus]] = {
    "canceled": (CancelOutcome.CANCELED, LinkedCancelStatus.CANCELED),
    "expired": (CancelOutcome.CANCELED, LinkedCancelStatus.CANCELED),
    "closed": (CancelOutcome.ALREADY_FILLED, LinkedCancelStatus.FILLED),
}
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class KrakenBracket:
    """Handle of a placed Kraken bracket; satisfies ``BracketHandle``.

    The conditional close has no client id of its own (Kraken assigns it a txid when the
    entry fills), so the exit client ids are ``None``.
    """

    group_id: int
    entry_client_id: int
    txid: str
    pair: str
    leg_count: int = 2

    @property
    def take_profit_client_id(self) -> None:
        return None

    @property
    def stop_loss_client_id(self) -> None:
        return None

    @property
    def emulated(self) -> bool:
        return False


def _next_group_id(owner: object) -> int:
    """The next free group id (1..GROUP_ID_MAX); seeded from the clock like the core's userrefs."""
    state = vars(owner)
    start = state.get("_bracket_group_counter")
    if start is None:
        start = int(time.time() * 1000) % KrakenUserrefLinkCodec.GROUP_ID_MAX
    state["_bracket_group_counter"] = start % KrakenUserrefLinkCodec.GROUP_ID_MAX + 1
    group_id: int = state["_bracket_group_counter"]
    return group_id


def _leg_fields(leg: LegSpec) -> dict[str, object]:
    """AddOrder fields (without ``pair``) for one leg, via the same path as ``place_order``."""
    return _order_fields(
        leg.order_type, leg.trade_type, leg.amount, leg.price, leg.limit_offset, False, leg.trigger
    )


def _close_fields(entry: LegSpec, exit_leg: LegSpec) -> dict[str, object]:
    """The ``close[...]`` fields for the single exit leg."""
    if exit_leg.amount != entry.amount:
        raise UnsupportedOrderError(
            exchange="kraken",
            feature="partial_exit",
            reason="a conditional close covers the entry's full volume",
        )
    if exit_leg.trade_type == entry.trade_type:
        raise ValueError("the exit must take the opposite trade_type of the entry")
    if exit_leg.order_type.name == "LIMIT_MAKER":
        raise UnsupportedOrderError(
            exchange="kraken",
            order_type=exit_leg.order_type.name,
            feature="post_only_exit",
            reason="close[] carries no oflags, so a post-only exit cannot be expressed",
        )
    fields = _leg_fields(exit_leg)
    if fields["ordertype"] not in _CLOSE_ORDERTYPES:
        raise UnsupportedOrderError(
            exchange="kraken",
            order_type=exit_leg.order_type.name,
            feature="conditional_close",
            reason=f"close[ordertype] must be one of {sorted(_CLOSE_ORDERTYPES)}",
        )
    return {
        f"close[{key}]": fields[key] for key in ("ordertype", "price", "price2") if key in fields
    }


def _cancel_outcome(
    handle: KrakenBracket,
    entry: CancelOutcome,
    terminal: bool,
    status: LinkedCancelStatus,
    residual: Decimal,
    exit_outcome: CancelOutcome | None,
) -> LinkedCancelOutcome:
    """Build the outcome; the exit leg (position 1) is ``None`` when it was never released."""
    rest: list[CancelOutcome | None] = [None] * (handle.leg_count - 1)
    if rest:
        rest[0] = exit_outcome
    return LinkedCancelOutcome(
        group_id=str(handle.group_id),
        all_legs_terminal=terminal,
        residual_filled=residual,
        leg_outcomes=(entry, *rest),
        status=status,
    )


def _group_id_option(options: Mapping[str, Any] | None, owner: object) -> int:
    given = (options or {}).get("group_id")
    if given is None:
        return _next_group_id(owner)
    if type(given) is not int:
        raise ValueError(f"group_id must be an int, got {type(given).__name__}")
    if not 0 <= given <= KrakenUserrefLinkCodec.GROUP_ID_MAX:
        raise ValueError(f"group_id {given} outside 0..{KrakenUserrefLinkCodec.GROUP_ID_MAX}")
    return given


async def _release_exit(owner: Any, handle: KrakenBracket) -> tuple[CancelOutcome, bool]:
    """Find and cancel the conditional close released by the entry's (partial) fill.

    The released exit has its own txid and inherits the entry's userref, so it is the open
    order with that userref other than the entry. Returns ``(exit outcome, terminal)``;
    when no exit is found or one cannot be cancelled the group is NOT terminal and the
    live exit txids are logged.
    """
    open_orders = await owner.get_open_orders(userref=handle.entry_client_id)
    exits = [txid for txid in open_orders if txid != handle.txid]
    if not exits:
        _LOG.warning(
            "bracket %s: entry %s filled but no released exit found under userref %s",
            handle.group_id,
            handle.txid,
            handle.entry_client_id,
        )
        return CancelOutcome.UNKNOWN, False
    live: list[str] = []
    for txid in exits:
        _add_open(owner, handle.pair, 1)  # released by the exchange, so not yet counted
        try:
            if not await owner.cancel_order(handle.pair, txid):
                live.append(txid)
        except OrderNotFoundError:
            continue  # already gone (filled or cancelled meanwhile)
        except Exception:
            _LOG.exception("bracket %s: could not cancel exit %s", handle.group_id, txid)
            live.append(txid)
    if live:
        _LOG.warning("bracket %s: exit order(s) still live: %s", handle.group_id, live)
        return CancelOutcome.UNKNOWN, False
    return CancelOutcome.CANCELED, True


class LinkedOrdersMixin:
    async def place_bracket(
        self: Any,
        target: str,
        spec: LinkedOrderSpec,
        *,
        options: Mapping[str, Any] | None = None,
    ) -> KrakenBracket:
        """Place the entry with its single exit as a native conditional close.

        ``options={"group_id": n}`` supplies the group id (a plain ``int``, default:
        allocated here). Allocated ids are unique within this process only: the sequence
        is seeded from the clock in milliseconds and repeats about every 37 hours. For
        long-lived GTC brackets pass ``group_id`` from your own allocator.

        Raises:
            GatewayNotStartedError: If the gateway is not ready.
            ValueError: If ``spec`` is not a BRACKET or a leg is malformed.
            UnsupportedOrderError: For more than one exit (Kraken allows one conditional
                close per order), a partial-volume exit or an exit type Kraken cannot close with.
            OrderOutcomeUnknownError: If the reply was lost; ``client_order_id`` is the
                entry userref, resolvable with ``get_open_orders(userref=...)``.
        """
        if not self.ready:
            raise GatewayNotStartedError("Gateway not started")
        if spec.kind is not LinkKind.BRACKET:
            raise ValueError(f"place_bracket requires a BRACKET spec, got {spec.kind.name}")
        entry = spec.entry
        exits = spec.exits
        if entry is None or not exits:
            raise ValueError("a BRACKET needs an ENTRY and one exit")
        if len(exits) > 1:
            raise UnsupportedOrderError(
                exchange="kraken",
                feature="bracket_two_exits",
                reason="Kraken allows one conditional close per order: "
                "pass a take-profit or a stop-loss, not both",
            )
        close = _close_fields(entry, exits[0])
        fields = _leg_fields(entry)
        group_id = _group_id_option(options, self)
        userref = _CODEC.encode(group_id, 0)  # the entry is ordered_legs()[0]
        pair = await resolve_wire_pair(self, target)
        body: dict[str, object] = {"pair": pair, **fields, **close, "userref": userref}
        txid = await _send_add_order(self, pair, body, userref)
        return KrakenBracket(
            group_id=group_id,
            entry_client_id=userref,
            txid=txid,
            pair=pair,
            leg_count=len(spec.ordered_legs()),
        )

    @unsupported_linked
    async def place_oco(
        self: Any,
        target: str,
        spec: LinkedOrderSpec,
        *,
        options: Mapping[str, Any] | None = None,
    ) -> Any:
        """Always raises: Kraken spot has no OCO (declared unsupported, nothing is sent).

        Raises:
            UnsupportedOrderError: Always, with ``feature="oco"``.
        """
        raise UnsupportedOrderError(
            exchange="kraken",
            feature="oco",
            reason="Kraken spot has no OCO; use place_bracket (conditional close)",
        )

    async def cancel_linked(self: Any, handle: KrakenBracket) -> LinkedCancelOutcome:
        """Cancel the bracket's entry, and its released exit if the entry (part-)filled.

        Cancelling an entry that already filled answers ``EOrder:Unknown order``; that is
        mapped through the order's status, not raised. Once the entry has filled, even
        partly, Kraken has released the conditional close as its own order (own txid, the
        entry's userref). It is found via ``get_open_orders(userref=...)`` and cancelled so
        the caller can flatten the position. The result is ``all_legs_terminal=True`` only
        if no exit can be live. If the exit is not found or cannot be cancelled it is
        ``False`` (exit leg outcome ``UNKNOWN``, the live txids logged at WARNING).
        ``residual_filled`` is the entry's executed volume (exit fills are not netted).
        """
        try:
            cancelled = await self.cancel_order(handle.pair, handle.txid)
        except OrderNotFoundError:
            cancelled = False  # already filled/closed; the status below decides
        info = (await self.query_orders([handle.txid])).get(handle.txid)
        if info is None:
            return _cancel_outcome(
                handle, CancelOutcome.NOT_FOUND, True, LinkedCancelStatus.UNKNOWN, Decimal(0), None
            )
        residual = Decimal(info.vol_exec)
        known = _AFTER_CANCEL.get(info.status)
        if known is None:
            status = LinkedCancelStatus.PENDING if cancelled else LinkedCancelStatus.UNKNOWN
            return _cancel_outcome(handle, CancelOutcome.UNKNOWN, False, status, residual, None)
        outcome, status = known
        if info.status != "closed" and residual <= 0:
            return _cancel_outcome(handle, outcome, True, status, residual, None)
        exit_outcome, terminal = await _release_exit(self, handle)
        if not terminal and status is LinkedCancelStatus.CANCELED:
            status = LinkedCancelStatus.FILLED
        return _cancel_outcome(handle, outcome, terminal, status, residual, exit_outcome)
