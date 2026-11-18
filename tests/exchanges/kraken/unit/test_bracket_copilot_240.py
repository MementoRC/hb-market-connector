"""Regression tests for the Copilot review of PR 279 (#240): T1, T2 and T4 (T3 lives in
``test_unknown_parent_240``).

Kraken facts pinned here: the conditional close is released only after the entry fills, inherits
the entry's userref and has its own txid; a finished exit is in ClosedOrders, not OpenOrders.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from data_type_primitives import CancelOutcome, LinkedCancelStatus

from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway
from market_connector.transport.response import Response
from tests.exchanges.kraken.unit import test_bracket_review_240 as review

_D = Decimal
_ENTRY, _EXIT, _PAIR = review._ENTRY, review._EXIT, review._PAIR  # noqa: SLF001
_UNKNOWN, _CANCELLED = review._UNKNOWN, review._CANCELLED  # noqa: SLF001
_spec = review._spec  # noqa: SLF001


def _info(status: str, vol_exec: str, userref: int, side: str = "sell") -> dict[str, Any]:
    return {
        "status": status,
        "vol": "0.5",
        "vol_exec": vol_exec,
        "userref": userref,
        "descr": {"pair": _PAIR, "type": side},
    }


class _Rest(review._Rest):  # noqa: SLF001
    """The review fake plus a ClosedOrders endpoint."""

    def __init__(self) -> None:
        super().__init__()
        self.closed: dict[str, dict[str, Any]] = {}

    async def request(
        self, endpoint_name: str, data: dict[str, Any] | None = None, **kw: Any
    ) -> Response:
        if endpoint_name != "closed_orders":
            return await super().request(endpoint_name, data, **kw)
        self.calls.append((endpoint_name, dict(data or {})))
        return Response(
            raw={"error": [], "result": {"closed": self.closed, "count": len(self.closed)}},
            status_code=200,
            headers=httpx.Headers(),
            _endpoint=endpoint_name,
        )


@pytest.fixture
def rest() -> _Rest:
    return _Rest()


@pytest.fixture
def gateway(rest: _Rest) -> KrakenGateway:
    gw = KrakenGateway(api_key="k", secret_key="c2VjcmV0")
    gw._rest = rest  # type: ignore[assignment]
    gw._started = True
    return gw


class TestMissingParentStillReleasesExitT1:
    async def test_parent_absent_from_query_orders_still_looks_up_open_exit(
        self, gateway: KrakenGateway, rest: _Rest
    ) -> None:
        handle = await gateway.place_bracket(_PAIR, _spec())
        rest.cancels[_ENTRY] = _UNKNOWN
        rest.cancels[_EXIT] = _CANCELLED
        rest.queried = {}  # QueryOrders does not know the parent
        rest.open = {_EXIT: _info("open", "0", handle.entry_client_id)}

        outcome = await gateway.cancel_linked(handle)

        assert _EXIT in rest.cancelled_txids()
        assert outcome.leg_outcomes[1] is CancelOutcome.CANCELED


class TestNetResidualT2:
    async def test_residual_is_entry_minus_exit_filled(
        self, gateway: KrakenGateway, rest: _Rest
    ) -> None:
        handle = await gateway.place_bracket(_PAIR, _spec())
        rest.cancels[_ENTRY] = _UNKNOWN
        rest.cancels[_EXIT] = _CANCELLED
        rest.queried = {_ENTRY: _info("closed", "0.5", handle.entry_client_id, "buy")}
        rest.open = {_EXIT: _info("open", "0.2", handle.entry_client_id)}

        outcome = await gateway.cancel_linked(handle)

        assert outcome.residual_filled == _D("0.3")


class TestClosedExitT4:
    async def test_completed_bracket_resolves_from_closed_orders(
        self, gateway: KrakenGateway, rest: _Rest
    ) -> None:
        handle = await gateway.place_bracket(_PAIR, _spec())
        rest.cancels[_ENTRY] = _UNKNOWN
        rest.queried = {_ENTRY: _info("closed", "0.5", handle.entry_client_id, "buy")}
        rest.open = {}
        rest.closed = {
            _ENTRY: _info("closed", "0.5", handle.entry_client_id, "buy"),
            _EXIT: _info("closed", "0.5", handle.entry_client_id),
        }

        outcome = await gateway.cancel_linked(handle)

        assert outcome.all_legs_terminal is True
        assert outcome.status is LinkedCancelStatus.FILLED
        assert outcome.leg_outcomes == (CancelOutcome.ALREADY_FILLED, CancelOutcome.ALREADY_FILLED)
        assert outcome.residual_filled == _D(0)
        assert rest.cancelled_txids() == [_ENTRY]  # nothing to cancel on the exit

    async def test_closed_cancelled_exit_is_terminal(
        self, gateway: KrakenGateway, rest: _Rest
    ) -> None:
        handle = await gateway.place_bracket(_PAIR, _spec())
        rest.cancels[_ENTRY] = _UNKNOWN
        rest.queried = {_ENTRY: _info("closed", "0.5", handle.entry_client_id, "buy")}
        rest.closed = {_EXIT: _info("canceled", "0", handle.entry_client_id)}

        outcome = await gateway.cancel_linked(handle)

        assert outcome.all_legs_terminal is True
        assert outcome.leg_outcomes[1] is CancelOutcome.CANCELED
        assert outcome.residual_filled == _D("0.5")
