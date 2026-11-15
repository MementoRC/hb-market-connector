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
* ``residual_filled`` is the sum over placed legs of ``filled_qty`` of the handle returned by
  ``cancel_order``; for a leg whose cancel raised, the leg's own pre-cancel ``filled_qty``.
* ``group_id == str(handle.group_id)`` (IB bracket ids are ints).
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from data_type_primitives import CancelOutcome, LinkedCancelOutcome

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


def _leg(cid: int, status: IBOrderState = IBOrderState.SUBMITTED, filled: str = "0") -> Any:
    """Stand-in for an ``OrderHandle`` (only client_order_id/status/filled_qty/owned are used)."""
    return SimpleNamespace(
        client_order_id=cid,
        order_id=cid,
        status=status,
        filled_qty=Decimal(filled),
        owned=True,
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


def _bracket(tp: bool = True, sl: bool = True, **entry_kw: Any) -> IbBracket:
    return IbBracket(
        parent=_leg(1, **entry_kw),
        take_profit=_leg(2) if tp else None,
        stop_loss=_leg(3) if sl else None,
    )


def _oca() -> IbOcaGroup:
    return IbOcaGroup(
        group="oca-1",
        oca_type=OcaType.CANCEL_WITH_BLOCK,
        orders=(_leg(7), _leg(8), _leg(9)),
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
    async def test_entry_fill_counts_as_residual(
        self, gateway: IbGatewayGateway, transport: MagicMock
    ) -> None:
        b = _bracket()
        _wire(transport, {1: _filled(b.parent, "10")})

        out = await gateway.cancel_linked(b)

        assert out.residual_filled == Decimal(10)
        assert isinstance(out.residual_filled, Decimal)

    async def test_residual_sums_fills_across_legs(
        self, gateway: IbGatewayGateway, transport: MagicMock
    ) -> None:
        b = _bracket()
        _wire(
            transport,
            {
                1: _filled(b.parent, "10"),
                2: _cancelled(b.take_profit, "1.5"),
                3: _cancelled(b.stop_loss),
            },
        )

        out = await gateway.cancel_linked(b)

        assert out.residual_filled == Decimal("11.5")

    async def test_partially_filled_then_cancelled_entry_reports_the_fill(
        self, gateway: IbGatewayGateway, transport: MagicMock
    ) -> None:
        b = _bracket()
        _wire(transport, {1: _cancelled(b.parent, "4")})

        out = await gateway.cancel_linked(b)

        assert out.leg_outcomes[0] is C.CANCELED
        assert out.residual_filled == Decimal(4)

    async def test_failed_leg_contributes_its_pre_cancel_fill(
        self, gateway: IbGatewayGateway, transport: MagicMock
    ) -> None:
        b = _bracket(filled="2")
        _wire(transport, {1: TimeoutError()})

        out = await gateway.cancel_linked(b)

        assert out.residual_filled == Decimal(2)

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

    async def test_residual_sums_fills_across_legs(
        self, gateway: IbGatewayGateway, transport: MagicMock
    ) -> None:
        g = _oca()
        _wire(transport, {7: _filled(g.orders[0], "3"), 8: _cancelled(g.orders[1], "0.5")})

        out = await gateway.cancel_linked(g)

        assert out.residual_filled == Decimal("3.5")
        assert isinstance(out.residual_filled, Decimal)
