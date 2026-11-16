"""IB ``cancel_linked`` behaviour for brackets and OCA groups (#251).

RED acceptance tests (goal-first TDD) for ``IbGatewayGateway.cancel_linked(handle)``.

Test double and interpretations (the implementation must honour these):

* The only IB cancel primitive is ``transport.cancel_order(leg_handle)``. ``cancel_linked``
  cancels each *placed* leg through it, exactly once, and never touches an absent leg.
  The mock returns a post-cancel handle (``status``, ``filled_qty``) or raises.
* Per-leg mapping of that call:
    returns status CANCELLED                       -> CANCELED
    returns status FILLED (incl. already filled)   -> ALREADY_FILLED
    raises IbError 10147 ("not found")             -> NOT_FOUND
    raises any other error (conn lost, timeout...) -> UNKNOWN
    returns a still-open status (SUBMITTED, ...)   -> UNKNOWN (ambiguous)
  ``cancel_linked`` never raises for a per-leg failure.
* ``len(leg_outcomes)`` equals the number of legs the group actually has, in dtp
  ``LinkedOrderSpec.ordered_legs()`` order (ENTRY, TAKE_PROFIT, STOP_LOSS, then OCO legs). The
  handle does not carry the spec, so a bracket's order is derived from its non-None ids in that
  same relative order: entry, then take_profit if set, then stop_loss if set. A single-exit
  bracket therefore has exactly 2 entries (entry, the one exit); there is no placeholder for the
  missing exit. For an OCA group the order follows ``handle.leg_client_ids``.
* ``None`` in dtp means "a leg in the group that was never placed/acknowledged", NOT "absent exit".
  The IB handles cannot express that (``IbBracket`` / ``IbOcaGroup`` only hold placed legs), so
  no ``None`` case is tested here.
* ``all_legs_terminal`` is True iff every outcome is CANCELED, ALREADY_FILLED or NOT_FOUND
  (UNKNOWN makes it False).
* ``residual_filled`` is dtp v0.13.0's SIGNED net open exposure in base quantity, relative to
  ``ordered_legs()[0]``'s side: a leg on the same side as the first leg ADDS its ``filled_qty``,
  a leg on the opposite side SUBTRACTS it (bracket: entry filled minus exits filled; may be
  negative). ``filled_qty`` is that of the handle returned by ``cancel_order``; for a leg whose
  cancel raised, the leg's own pre-cancel ``filled_qty``. A leg's side is read from the IB
  handle's ``OrderHandle.action`` (``"BUY"``/``"SELL"``); the handles carry no other side.
* ``group_id == str(handle.group_id)`` (IB bracket ids are ints).
* ``status`` (dtp v0.13.0 ``LinkedCancelStatus``) is derived from the per-leg outcomes, first
  matching rule wins:
    1. any leg ALREADY_FILLED                          -> FILLED (even with other legs open)
    2. every leg CANCELED or NOT_FOUND (terminal)      -> CANCELED
    3. any leg's cancel raised a non-not-found error   -> UNKNOWN (beats PENDING)
    4. any leg came back from cancel still open        -> PENDING (requested, unconfirmed)
  dtp validators: PENDING needs ``all_legs_terminal=False``; CANCELED and FAILED need
  ``all_legs_terminal=True``; FILLED and UNKNOWN are allowed either way.
* FAILED is never produced by a native IB ``cancel_linked``: it describes a group-level
  submission failure in strategy-framework's emulator, which a native cancel does not observe.
"""

from __future__ import annotations

import itertools
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from data_type_primitives import CancelOutcome, LinkedCancelOutcome, LinkedCancelStatus

from market_connector.exchanges.interactive_brokers.exceptions import ConnectionLostError, IbError
from market_connector.exchanges.interactive_brokers.ib_gateway import IbGatewayGateway
from market_connector.exchanges.interactive_brokers.linked_orders import (
    IbBracket,
    IbOcaGroup,
    OcaType,
)
from market_connector.exchanges.interactive_brokers.order_handle import IBOrderState

pytest.importorskip("ib_async")

C = CancelOutcome
_TERMINAL = {C.CANCELED, C.ALREADY_FILLED, C.NOT_FOUND}


def _leg(
    cid: int,
    status: IBOrderState = IBOrderState.SUBMITTED,
    filled: str = "0",
    action: str = "BUY",
) -> Any:
    """Stand-in for an ``OrderHandle``.

    Only client_order_id/status/filled_qty/owned/action are used. ``action`` is the real
    ``OrderHandle.action`` property (``"BUY"`` or ``"SELL"``), the only per-leg side the IB
    handles expose; ``cancel_linked`` must read each leg's side from it.
    """
    return SimpleNamespace(
        client_order_id=cid,
        order_id=cid,
        status=status,
        filled_qty=Decimal(filled),
        owned=True,
        action=action,
    )


def _after(leg: Any, status: IBOrderState, filled: str | None = None) -> Any:
    qty = leg.filled_qty if filled is None else Decimal(filled)
    return SimpleNamespace(**{**vars(leg), "status": status, "filled_qty": qty})


def _cancelled(leg: Any, filled: str | None = None) -> Any:
    return _after(leg, IBOrderState.CANCELLED, filled)


def _filled(leg: Any, filled: str = "10") -> Any:
    return _after(leg, IBOrderState.FILLED, filled)


def _not_found() -> Exception:
    return IbError(10147, "OrderId that needs to be cancelled is not found")


def _wire(transport: MagicMock, results: dict[int, Any]) -> None:
    """``cancel_order`` answers per client order id; unlisted legs cancel cleanly."""

    async def cancel_order(handle: Any) -> Any:
        result = results.get(handle.client_order_id, _cancelled(handle))
        if isinstance(result, Exception):
            raise result
        return result

    transport.cancel_order = AsyncMock(side_effect=cancel_order)


_OPPOSITE = {"BUY": "SELL", "SELL": "BUY"}


def _bracket(
    tp: bool = True,
    sl: bool = True,
    entry_action: str = "BUY",
    exit_filled: str = "0",
    **entry_kw: Any,
) -> IbBracket:
    """Entry on ``entry_action``; exits on the opposite side (dtp bracket rule)."""
    exit_action = _OPPOSITE[entry_action]
    return IbBracket(
        parent=_leg(1, action=entry_action, **entry_kw),
        take_profit=_leg(2, filled=exit_filled, action=exit_action) if tp else None,
        stop_loss=_leg(3, filled=exit_filled, action=exit_action) if sl else None,
    )


def _oca(actions: tuple[str, str, str] = ("SELL", "SELL", "SELL")) -> IbOcaGroup:
    return IbOcaGroup(
        group="oca-1",
        oca_type=OcaType.CANCEL_WITH_BLOCK,
        orders=(
            _leg(7, action=actions[0]),
            _leg(8, action=actions[1]),
            _leg(9, action=actions[2]),
        ),
    )


def _cancelled_ids(transport: MagicMock) -> list[int]:
    return sorted(c.args[0].client_order_id for c in transport.cancel_order.await_args_list)


@pytest.fixture
def transport() -> MagicMock:
    t = MagicMock()
    _wire(t, {})
    return t


@pytest.fixture
def gateway(transport: MagicMock) -> IbGatewayGateway:
    return IbGatewayGateway(transport=transport, signer=MagicMock(), contract_resolver=MagicMock())


# result name -> (factory(leg) -> cancel_order result, expected CancelOutcome)
_MAPPING: dict[str, tuple[Any, CancelOutcome]] = {
    "cancelled": (_cancelled, C.CANCELED),
    "filled": (_filled, C.ALREADY_FILLED),
    "not_found": (lambda leg: _not_found(), C.NOT_FOUND),
    "connection_lost": (lambda leg: ConnectionLostError(1100, "connectivity lost"), C.UNKNOWN),
    "timeout": (lambda leg: TimeoutError(), C.UNKNOWN),
    "still_open": (lambda leg: _after(leg, IBOrderState.SUBMITTED), C.UNKNOWN),
}


class TestBracketPerLegMapping:
    @pytest.mark.parametrize("name", list(_MAPPING))
    async def test_each_leg_maps_the_transport_result(
        self, gateway: IbGatewayGateway, transport: MagicMock, name: str
    ) -> None:
        make, expected = _MAPPING[name]
        b = _bracket()
        _wire(transport, {2: make(b.take_profit)})

        out = await gateway.cancel_linked(b)

        assert out.leg_outcomes == (C.CANCELED, expected, C.CANCELED)

    async def test_returns_dtp_linked_cancel_outcome(self, gateway: IbGatewayGateway) -> None:
        out = await gateway.cancel_linked(_bracket())

        assert isinstance(out, LinkedCancelOutcome)
        assert isinstance(out.leg_outcomes, tuple)

    async def test_clean_cancel_reports_all_canceled_and_terminal(
        self, gateway: IbGatewayGateway
    ) -> None:
        out = await gateway.cancel_linked(_bracket())

        assert out.leg_outcomes == (C.CANCELED, C.CANCELED, C.CANCELED)
        assert out.all_legs_terminal is True
        assert out.residual_filled == Decimal(0)


class TestBracketGroupIdAndLegOrder:
    async def test_group_id_is_str_of_int_parent_client_id(self, gateway: IbGatewayGateway) -> None:
        out = await gateway.cancel_linked(_bracket())

        assert isinstance(out.group_id, str)
        assert out.group_id == "1"

    async def test_leg_order_is_entry_take_profit_stop_loss_as_in_ordered_legs(
        self, gateway: IbGatewayGateway, transport: MagicMock
    ) -> None:
        b = _bracket()
        _wire(transport, {1: _filled(b.parent), 2: _cancelled(b.take_profit), 3: _not_found()})

        out = await gateway.cancel_linked(b)

        assert out.leg_outcomes == (C.ALREADY_FILLED, C.CANCELED, C.NOT_FOUND)

    async def test_tp_only_bracket_has_two_outcomes_entry_then_take_profit(
        self, gateway: IbGatewayGateway, transport: MagicMock
    ) -> None:
        b = _bracket(sl=False)
        _wire(transport, {1: _filled(b.parent), 2: _not_found()})

        out = await gateway.cancel_linked(b)

        assert out.leg_outcomes == (C.ALREADY_FILLED, C.NOT_FOUND)

    async def test_sl_only_bracket_has_two_outcomes_entry_then_stop_loss(
        self, gateway: IbGatewayGateway, transport: MagicMock
    ) -> None:
        b = _bracket(tp=False)
        _wire(transport, {1: _filled(b.parent), 3: _not_found()})

        out = await gateway.cancel_linked(b)

        assert out.leg_outcomes == (C.ALREADY_FILLED, C.NOT_FOUND)

    async def test_entry_only_bracket_has_a_single_outcome(self, gateway: IbGatewayGateway) -> None:
        out = await gateway.cancel_linked(_bracket(tp=False, sl=False))

        assert out.leg_outcomes == (C.CANCELED,)

    async def test_absent_leg_is_never_sent_to_the_transport(
        self, gateway: IbGatewayGateway, transport: MagicMock
    ) -> None:
        await gateway.cancel_linked(_bracket(tp=False))

        assert _cancelled_ids(transport) == [1, 3]

    async def test_every_placed_leg_is_cancelled_exactly_once(
        self, gateway: IbGatewayGateway, transport: MagicMock
    ) -> None:
        await gateway.cancel_linked(_bracket())

        assert _cancelled_ids(transport) == [1, 2, 3]


class TestBracketTerminalFlag:
    @pytest.mark.parametrize("name", [n for n, (_, o) in _MAPPING.items() if o in _TERMINAL])
    async def test_terminal_outcomes_make_all_legs_terminal(
        self, gateway: IbGatewayGateway, transport: MagicMock, name: str
    ) -> None:
        make, _ = _MAPPING[name]
        b = _bracket()
        _wire(transport, {1: make(b.parent), 2: make(b.take_profit), 3: make(b.stop_loss)})

        out = await gateway.cancel_linked(b)

        assert out.all_legs_terminal is True

    @pytest.mark.parametrize("name", [n for n, (_, o) in _MAPPING.items() if o is C.UNKNOWN])
    async def test_unknown_leg_makes_group_non_terminal(
        self, gateway: IbGatewayGateway, transport: MagicMock, name: str
    ) -> None:
        make, _ = _MAPPING[name]
        b = _bracket()
        _wire(transport, {3: make(b.stop_loss)})

        out = await gateway.cancel_linked(b)

        assert out.leg_outcomes == (C.CANCELED, C.CANCELED, C.UNKNOWN)
        assert out.all_legs_terminal is False

    async def test_mixed_terminal_outcomes_are_terminal(
        self, gateway: IbGatewayGateway, transport: MagicMock
    ) -> None:
        b = _bracket()
        _wire(transport, {1: _filled(b.parent), 2: _cancelled(b.take_profit), 3: _not_found()})

        out = await gateway.cancel_linked(b)

        assert out.all_legs_terminal is True

    async def test_absent_exits_do_not_block_terminal(self, gateway: IbGatewayGateway) -> None:
        out = await gateway.cancel_linked(_bracket(tp=False, sl=False))

        assert out.leg_outcomes == (C.CANCELED,)
        assert out.all_legs_terminal is True

    async def test_single_exit_bracket_with_unknown_exit_is_non_terminal(
        self, gateway: IbGatewayGateway, transport: MagicMock
    ) -> None:
        _wire(transport, {2: TimeoutError()})

        out = await gateway.cancel_linked(_bracket(sl=False))

        assert out.leg_outcomes == (C.CANCELED, C.UNKNOWN)
        assert out.all_legs_terminal is False


class TestBracketDoesNotRaise:
    async def test_failing_leg_is_reported_not_raised(
        self, gateway: IbGatewayGateway, transport: MagicMock
    ) -> None:
        _wire(transport, {1: ConnectionLostError(1100, "connectivity lost")})

        out = await gateway.cancel_linked(_bracket())

        assert out.leg_outcomes[0] is C.UNKNOWN

    async def test_other_legs_are_still_cancelled_after_a_failure(
        self, gateway: IbGatewayGateway, transport: MagicMock
    ) -> None:
        _wire(transport, {1: TimeoutError()})

        out = await gateway.cancel_linked(_bracket())

        assert _cancelled_ids(transport) == [1, 2, 3]
        assert out.leg_outcomes == (C.UNKNOWN, C.CANCELED, C.CANCELED)

    async def test_every_leg_failing_still_returns_an_outcome(
        self, gateway: IbGatewayGateway, transport: MagicMock
    ) -> None:
        err = ConnectionLostError(1100, "connectivity lost")
        _wire(transport, {1: err, 2: err, 3: err})

        out = await gateway.cancel_linked(_bracket())

        assert out.leg_outcomes == (C.UNKNOWN, C.UNKNOWN, C.UNKNOWN)
        assert out.all_legs_terminal is False


class TestBracketResidualFilled:
    async def test_entry_filled_and_exits_unfilled_is_the_entry_fill(
        self, gateway: IbGatewayGateway, transport: MagicMock
    ) -> None:
        b = _bracket()
        _wire(transport, {1: _filled(b.parent, "0.5")})

        out = await gateway.cancel_linked(b)

        assert out.residual_filled == Decimal("0.5")
        assert isinstance(out.residual_filled, Decimal)

    async def test_entry_and_take_profit_both_filled_nets_to_zero(
        self, gateway: IbGatewayGateway, transport: MagicMock
    ) -> None:
        b = _bracket()
        _wire(transport, {1: _filled(b.parent, "1"), 2: _filled(b.take_profit, "1")})

        out = await gateway.cancel_linked(b)

        assert out.residual_filled == Decimal(0)

    async def test_partial_take_profit_then_cancel_leaves_the_remainder(
        self, gateway: IbGatewayGateway, transport: MagicMock
    ) -> None:
        b = _bracket()
        _wire(
            transport,
            {1: _filled(b.parent, "1"), 2: _cancelled(b.take_profit, "0.4")},
        )

        out = await gateway.cancel_linked(b)

        assert out.residual_filled == Decimal("0.6")

    async def test_double_exit_fill_is_negative(
        self, gateway: IbGatewayGateway, transport: MagicMock
    ) -> None:
        b = _bracket()
        _wire(
            transport,
            {
                1: _filled(b.parent, "1"),
                2: _filled(b.take_profit, "1"),
                3: _filled(b.stop_loss, "1"),
            },
        )

        out = await gateway.cancel_linked(b)

        assert out.residual_filled == Decimal(-1)

    async def test_sign_is_relative_to_a_sell_entry(
        self, gateway: IbGatewayGateway, transport: MagicMock
    ) -> None:
        b = _bracket(entry_action="SELL")
        _wire(
            transport,
            {1: _filled(b.parent, "1"), 2: _cancelled(b.take_profit, "0.4")},
        )

        out = await gateway.cancel_linked(b)

        assert out.residual_filled == Decimal("0.6")

    async def test_partially_filled_then_cancelled_entry_reports_the_fill(
        self, gateway: IbGatewayGateway, transport: MagicMock
    ) -> None:
        b = _bracket()
        _wire(transport, {1: _cancelled(b.parent, "4")})

        out = await gateway.cancel_linked(b)

        assert out.leg_outcomes[0] is C.CANCELED
        assert out.residual_filled == Decimal(4)

    async def test_failed_entry_contributes_its_pre_cancel_fill(
        self, gateway: IbGatewayGateway, transport: MagicMock
    ) -> None:
        b = _bracket(filled="2")
        _wire(transport, {1: TimeoutError()})

        out = await gateway.cancel_linked(b)

        assert out.residual_filled == Decimal(2)

    async def test_failed_exit_subtracts_its_pre_cancel_fill(
        self, gateway: IbGatewayGateway, transport: MagicMock
    ) -> None:
        b = _bracket(sl=False, exit_filled="0.5", filled="2")
        _wire(transport, {2: TimeoutError()})

        out = await gateway.cancel_linked(b)

        assert out.leg_outcomes == (C.CANCELED, C.UNKNOWN)
        assert out.residual_filled == Decimal("1.5")

    async def test_no_fills_is_zero(self, gateway: IbGatewayGateway) -> None:
        out = await gateway.cancel_linked(_bracket())

        assert out.residual_filled == Decimal(0)


class TestOcaCancelLinked:
    async def test_group_id_is_the_oca_group_name(self, gateway: IbGatewayGateway) -> None:
        out = await gateway.cancel_linked(_oca())

        assert out.group_id == "oca-1"
        assert isinstance(out, LinkedCancelOutcome)

    async def test_leg_outcomes_follow_leg_client_ids_order(
        self, gateway: IbGatewayGateway, transport: MagicMock
    ) -> None:
        g = _oca()
        _wire(transport, {7: _filled(g.orders[0]), 8: _not_found(), 9: TimeoutError()})

        out = await gateway.cancel_linked(g)

        assert g.leg_client_ids == (7, 8, 9)
        assert out.leg_outcomes == (C.ALREADY_FILLED, C.NOT_FOUND, C.UNKNOWN)

    @pytest.mark.parametrize("name", list(_MAPPING))
    async def test_each_leg_maps_the_transport_result(
        self, gateway: IbGatewayGateway, transport: MagicMock, name: str
    ) -> None:
        make, expected = _MAPPING[name]
        g = _oca()
        _wire(transport, {8: make(g.orders[1])})

        out = await gateway.cancel_linked(g)

        assert out.leg_outcomes == (C.CANCELED, expected, C.CANCELED)

    async def test_every_leg_is_cancelled_exactly_once(
        self, gateway: IbGatewayGateway, transport: MagicMock
    ) -> None:
        await gateway.cancel_linked(_oca())

        assert _cancelled_ids(transport) == [7, 8, 9]

    async def test_terminal_when_every_leg_is_terminal(
        self, gateway: IbGatewayGateway, transport: MagicMock
    ) -> None:
        g = _oca()
        _wire(transport, {7: _filled(g.orders[0]), 8: _not_found()})

        out = await gateway.cancel_linked(g)

        assert out.leg_outcomes == (C.ALREADY_FILLED, C.NOT_FOUND, C.CANCELED)
        assert out.all_legs_terminal is True

    async def test_unknown_leg_is_non_terminal_and_does_not_raise(
        self, gateway: IbGatewayGateway, transport: MagicMock
    ) -> None:
        _wire(transport, {9: ConnectionLostError(1100, "connectivity lost")})

        out = await gateway.cancel_linked(_oca())

        assert out.leg_outcomes == (C.CANCELED, C.CANCELED, C.UNKNOWN)
        assert out.all_legs_terminal is False
        assert _cancelled_ids(transport) == [7, 8, 9]

    async def test_same_side_oca_sums_fills(
        self, gateway: IbGatewayGateway, transport: MagicMock
    ) -> None:
        g = _oca()
        _wire(transport, {7: _filled(g.orders[0], "3"), 8: _cancelled(g.orders[1], "0.5")})

        out = await gateway.cancel_linked(g)

        assert out.residual_filled == Decimal("3.5")
        assert isinstance(out.residual_filled, Decimal)

    async def test_breakout_oca_double_fill_nets_to_zero(
        self, gateway: IbGatewayGateway, transport: MagicMock
    ) -> None:
        g = _oca(("BUY", "SELL", "BUY"))
        _wire(transport, {7: _filled(g.orders[0], "1"), 8: _filled(g.orders[1], "1")})

        out = await gateway.cancel_linked(g)

        assert out.residual_filled == Decimal(0)

    async def test_same_side_as_first_leg_winner_is_positive(
        self, gateway: IbGatewayGateway, transport: MagicMock
    ) -> None:
        g = _oca(("BUY", "SELL", "BUY"))
        _wire(transport, {7: _filled(g.orders[0], "1")})

        out = await gateway.cancel_linked(g)

        assert out.residual_filled == Decimal(1)

    async def test_opposite_side_winner_is_negative(
        self, gateway: IbGatewayGateway, transport: MagicMock
    ) -> None:
        g = _oca(("BUY", "SELL", "BUY"))
        _wire(transport, {8: _filled(g.orders[1], "1")})

        out = await gateway.cancel_linked(g)

        assert out.residual_filled == Decimal(-1)

    async def test_no_fills_is_zero(self, gateway: IbGatewayGateway) -> None:
        out = await gateway.cancel_linked(_oca(("BUY", "SELL", "BUY")))

        assert out.residual_filled == Decimal(0)


S = LinkedCancelStatus


def _open(leg: Any) -> Any:
    return _after(leg, IBOrderState.SUBMITTED)


def _err() -> Exception:
    return TimeoutError()


# id -> (group factory, results(group) -> {client_id: result}, expected status, expected terminal)
_STATUS_CASES: dict[str, tuple[Any, Any, LinkedCancelStatus, bool]] = {
    "bracket_all_canceled": (_bracket, lambda g: {}, S.CANCELED, True),
    "bracket_all_not_found": (
        _bracket,
        lambda g: {1: _not_found(), 2: _not_found(), 3: _not_found()},
        S.CANCELED,
        True,
    ),
    "bracket_tp_filled_sl_canceled": (
        _bracket,
        lambda g: {2: _filled(g.take_profit)},
        S.FILLED,
        True,
    ),
    "bracket_tp_filled_sl_errored": (
        _bracket,
        lambda g: {2: _filled(g.take_profit), 3: _err()},
        S.FILLED,
        False,
    ),
    "bracket_tp_filled_sl_open": (
        _bracket,
        lambda g: {2: _filled(g.take_profit), 3: _open(g.stop_loss)},
        S.FILLED,
        False,
    ),
    "bracket_entry_filled_exits_canceled": (
        _bracket,
        lambda g: {1: _filled(g.parent)},
        S.FILLED,
        True,
    ),
    "bracket_exit_errored_rest_canceled": (_bracket, lambda g: {3: _err()}, S.UNKNOWN, False),
    "bracket_exit_open_rest_canceled": (
        _bracket,
        lambda g: {3: _open(g.stop_loss)},
        S.PENDING,
        False,
    ),
    "bracket_one_open_one_errored": (
        _bracket,
        lambda g: {2: _open(g.take_profit), 3: _err()},
        S.UNKNOWN,
        False,
    ),
    "bracket_tp_only_canceled": (lambda: _bracket(sl=False), lambda g: {}, S.CANCELED, True),
    "bracket_sl_only_canceled": (lambda: _bracket(tp=False), lambda g: {}, S.CANCELED, True),
    "bracket_tp_only_filled": (
        lambda: _bracket(sl=False),
        lambda g: {2: _filled(g.take_profit)},
        S.FILLED,
        True,
    ),
    "bracket_sl_only_filled": (
        lambda: _bracket(tp=False),
        lambda g: {3: _filled(g.stop_loss)},
        S.FILLED,
        True,
    ),
    "oco_all_canceled": (_oca, lambda g: {}, S.CANCELED, True),
    "oco_winner_filled": (_oca, lambda g: {8: _filled(g.orders[1])}, S.FILLED, True),
    "oco_one_errored": (_oca, lambda g: {9: _err()}, S.UNKNOWN, False),
    "oco_one_open": (_oca, lambda g: {9: _open(g.orders[2])}, S.PENDING, False),
}


class TestCancelLinkedStatus:
    @pytest.mark.parametrize("case", list(_STATUS_CASES))
    async def test_status_is_derived_from_leg_outcomes(
        self, gateway: IbGatewayGateway, transport: MagicMock, case: str
    ) -> None:
        make, results, status, terminal = _STATUS_CASES[case]
        g = make()
        _wire(transport, results(g))

        out = await gateway.cancel_linked(g)

        assert out.status is status
        assert out.all_legs_terminal is terminal
        # The expected pair must itself be valid under dtp's validators (raises otherwise).
        LinkedCancelOutcome(
            group_id="g",
            all_legs_terminal=terminal,
            residual_filled=Decimal(0),
            leg_outcomes=(C.CANCELED,),
            status=status,
        )

    @pytest.mark.parametrize("case", list(_STATUS_CASES))
    async def test_returned_outcome_round_trips_through_dtp_validation(
        self, gateway: IbGatewayGateway, transport: MagicMock, case: str
    ) -> None:
        make, results, _, _ = _STATUS_CASES[case]
        g = make()
        _wire(transport, results(g))

        out = await gateway.cancel_linked(g)

        assert LinkedCancelOutcome.model_validate(out.model_dump()) == out

    async def test_failed_is_never_produced_by_native_cancel_linked(
        self, gateway: IbGatewayGateway, transport: MagicMock
    ) -> None:
        statuses: set[LinkedCancelStatus | None] = set()
        for names in itertools.product(_MAPPING, repeat=3):
            for make in (_bracket, _oca):
                g = make()
                legs = (g.parent, g.take_profit, g.stop_loss) if make is _bracket else g.orders
                _wire(
                    transport,
                    {
                        leg.client_order_id: _MAPPING[n][0](leg)
                        for n, leg in zip(names, legs, strict=True)
                    },
                )
                statuses.add((await gateway.cancel_linked(g)).status)

        assert S.FAILED not in statuses
        assert None not in statuses
