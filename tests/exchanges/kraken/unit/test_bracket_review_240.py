"""Regression tests for the review findings on #240 (F2-F4, F6-F9).

Kraken facts pinned here: cancelling an order that already filled answers
``EOrder:Unknown order``; once an entry fills, the conditional close is released as its own
order (own txid) that inherits the entry's userref.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import httpx
import pytest
from data_type_primitives import (
    CancelOutcome,
    LegRole,
    LegSpec,
    LinkedCancelStatus,
    LinkedOrderSpec,
    LinkKind,
    TriggerKind,
    TriggerSpec,
)
from data_type_primitives.common import OrderType, TradeType

from market_connector.capabilities import LinkedMode, OrderCapabilities, unsupported_linked
from market_connector.exceptions import UnsupportedOrderError
from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway
from market_connector.exchanges.kraken.mixins.orders import _open_counts
from market_connector.testing.contract import check_linked_protocol_capabilities
from market_connector.transport.response import Response

_D = Decimal
_PAIR = "XBTUSD"
_ENTRY = "OENTRY-XXXXX-YYYYYY"
_EXIT = "OEXIT0-XXXXX-ZZZZZZ"
_UNKNOWN = {"error": ["EOrder:Unknown order"]}
_CANCELLED = {"error": [], "result": {"count": 1}}


def _info(status: str, vol_exec: str, userref: int) -> dict[str, Any]:
    return {
        "status": status,
        "vol": "0.5",
        "vol_exec": vol_exec,
        "userref": userref,
        "descr": {"pair": _PAIR},
    }


class _Rest:
    """Routes by endpoint name; ``cancels`` maps txid -> raw cancel reply."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.cancels: dict[str, dict[str, Any]] = {}
        self.queried: dict[str, dict[str, Any]] = {}
        self.open: dict[str, dict[str, Any]] = {}

    async def request(
        self, endpoint_name: str, data: dict[str, Any] | None = None, **_: Any
    ) -> Response:
        body = dict(data or {})
        self.calls.append((endpoint_name, body))
        raw: dict[str, Any]
        if endpoint_name == "add_order":
            raw = {"error": [], "result": {"descr": {"order": "x"}, "txid": [_ENTRY]}}
        elif endpoint_name == "cancel_order":
            raw = self.cancels[body["txid"]]
        elif endpoint_name == "query_orders":
            raw = {"error": [], "result": self.queried}
        elif endpoint_name == "open_orders":
            raw = {"error": [], "result": {"open": self.open}}
        else:
            raise AssertionError(f"unexpected endpoint {endpoint_name}")
        return Response(raw=raw, status_code=200, headers=httpx.Headers(), _endpoint=endpoint_name)

    def cancelled_txids(self) -> list[str]:
        return [body["txid"] for name, body in self.calls if name == "cancel_order"]

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


@pytest.fixture
def rest() -> _Rest:
    return _Rest()


@pytest.fixture
def gateway(rest: _Rest) -> KrakenGateway:
    gw = KrakenGateway(api_key="k", secret_key="c2VjcmV0")
    gw._rest = rest  # type: ignore[assignment]
    gw._started = True
    return gw


def _leg(
    role: LegRole,
    order_type: OrderType,
    side: TradeType,
    *,
    price: str | None = None,
    trigger: str | None = None,
) -> LegSpec:
    return LegSpec(
        role=role,
        order_type=order_type,
        trade_type=side,
        amount=_D("0.5"),
        price=None if price is None else _D(price),
        trigger=None
        if trigger is None
        else TriggerSpec(kind=TriggerKind.ABSOLUTE, value=_D(trigger)),
    )


def _spec(exit_leg: LegSpec | None = None) -> LinkedOrderSpec:
    entry = _leg(LegRole.ENTRY, OrderType.LIMIT, TradeType.BUY, price="50000")
    stop = exit_leg or _leg(LegRole.STOP_LOSS, OrderType.STOP_LOSS, TradeType.SELL, trigger="49000")
    return LinkedOrderSpec(kind=LinkKind.BRACKET, legs=(entry, stop))


# --------------------------------------------------------------------------- F2


class TestCancelAfterFillF2:
    async def test_cancel_of_filled_entry_yields_outcome_not_exception(
        self, gateway: KrakenGateway, rest: _Rest
    ) -> None:
        handle = await gateway.place_bracket(_PAIR, _spec())
        rest.cancels[_ENTRY] = _UNKNOWN
        rest.cancels[_EXIT] = _CANCELLED
        rest.queried = {_ENTRY: _info("closed", "0.5", handle.entry_client_id)}
        rest.open = {_EXIT: _info("open", "0", handle.entry_client_id)}

        outcome = await gateway.cancel_linked(handle)

        assert outcome.leg_outcomes[0] is CancelOutcome.ALREADY_FILLED
        assert outcome.status is LinkedCancelStatus.FILLED

    async def test_unknown_order_path_does_not_decrement_open_counter(
        self, gateway: KrakenGateway, rest: _Rest
    ) -> None:
        handle = await gateway.place_bracket(_PAIR, _spec())
        before = _open_counts(gateway)[_PAIR]
        rest.cancels[_ENTRY] = _UNKNOWN
        rest.queried = {_ENTRY: _info("canceled", "0", handle.entry_client_id)}

        await gateway.cancel_linked(handle)

        assert _open_counts(gateway)[_PAIR] == before


# --------------------------------------------------------------------------- F3


class TestReleasedExitF3:
    async def test_filled_entry_exit_is_found_by_userref_and_cancelled(
        self, gateway: KrakenGateway, rest: _Rest
    ) -> None:
        handle = await gateway.place_bracket(_PAIR, _spec())
        rest.cancels[_ENTRY] = _UNKNOWN
        rest.cancels[_EXIT] = _CANCELLED
        rest.queried = {_ENTRY: _info("closed", "0.5", handle.entry_client_id)}
        rest.open = {
            _ENTRY: _info("closed", "0.5", handle.entry_client_id),
            _EXIT: _info("open", "0", handle.entry_client_id),
        }

        outcome = await gateway.cancel_linked(handle)

        assert [b for n, b in rest.calls if n == "open_orders"] == [
            {"userref": handle.entry_client_id}
        ]
        assert rest.cancelled_txids() == [_ENTRY, _EXIT]
        assert outcome.all_legs_terminal is True
        assert outcome.leg_outcomes == (CancelOutcome.ALREADY_FILLED, CancelOutcome.CANCELED)
        assert outcome.residual_filled == _D("0.5")

    async def test_exit_not_found_is_not_terminal(
        self, gateway: KrakenGateway, rest: _Rest
    ) -> None:
        handle = await gateway.place_bracket(_PAIR, _spec())
        rest.cancels[_ENTRY] = _UNKNOWN
        rest.queried = {_ENTRY: _info("closed", "0.5", handle.entry_client_id)}
        rest.open = {}

        outcome = await gateway.cancel_linked(handle)

        assert outcome.all_legs_terminal is False

    async def test_exit_cancel_failure_is_not_terminal_and_names_exit(
        self,
        gateway: KrakenGateway,
        rest: _Rest,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        handle = await gateway.place_bracket(_PAIR, _spec())
        rest.cancels[_ENTRY] = _UNKNOWN
        rest.cancels[_EXIT] = {"error": ["EService:Unavailable"]}
        rest.queried = {_ENTRY: _info("closed", "0.5", handle.entry_client_id)}
        rest.open = {_EXIT: _info("open", "0", handle.entry_client_id)}

        with caplog.at_level(logging.WARNING):
            outcome = await gateway.cancel_linked(handle)

        assert outcome.all_legs_terminal is False
        assert outcome.leg_outcomes[1] is CancelOutcome.UNKNOWN
        assert _EXIT in caplog.text


# --------------------------------------------------------------------------- F4


class TestPartialEntryF4:
    @pytest.mark.parametrize("status", ["canceled", "expired"])
    async def test_partially_filled_entry_exit_is_cancelled_and_residual_reported(
        self, gateway: KrakenGateway, rest: _Rest, status: str
    ) -> None:
        handle = await gateway.place_bracket(_PAIR, _spec())
        rest.cancels[_ENTRY] = _CANCELLED
        rest.cancels[_EXIT] = _CANCELLED
        rest.queried = {_ENTRY: _info(status, "0.2", handle.entry_client_id)}
        rest.open = {_EXIT: _info("open", "0", handle.entry_client_id)}

        outcome = await gateway.cancel_linked(handle)

        assert _EXIT in rest.cancelled_txids()
        assert outcome.residual_filled == _D("0.2")
        assert outcome.all_legs_terminal is True

    async def test_partially_filled_entry_with_live_exit_is_not_terminal(
        self, gateway: KrakenGateway, rest: _Rest
    ) -> None:
        handle = await gateway.place_bracket(_PAIR, _spec())
        rest.cancels[_ENTRY] = _CANCELLED
        rest.cancels[_EXIT] = {"error": ["EService:Unavailable"]}
        rest.queried = {_ENTRY: _info("canceled", "0.2", handle.entry_client_id)}
        rest.open = {_EXIT: _info("open", "0", handle.entry_client_id)}

        outcome = await gateway.cancel_linked(handle)

        assert outcome.all_legs_terminal is False
        assert outcome.residual_filled == _D("0.2")

    async def test_unfilled_cancelled_entry_needs_no_exit_search(
        self, gateway: KrakenGateway, rest: _Rest
    ) -> None:
        handle = await gateway.place_bracket(_PAIR, _spec())
        rest.cancels[_ENTRY] = _CANCELLED
        rest.queried = {_ENTRY: _info("canceled", "0", handle.entry_client_id)}

        outcome = await gateway.cancel_linked(handle)

        assert outcome.all_legs_terminal is True
        assert "open_orders" not in rest.names()


# --------------------------------------------------------------------------- F6


class TestGroupIdValidationF6:
    @pytest.mark.parametrize("bad", [True, 1.0, 5.9, "5", 0 - 1, 2**27])
    async def test_bad_group_id_is_rejected_before_any_request(
        self, gateway: KrakenGateway, rest: _Rest, bad: Any
    ) -> None:
        with pytest.raises(ValueError, match="group_id"):
            await gateway.place_bracket(_PAIR, _spec(), options={"group_id": bad})
        assert rest.calls == []


# --------------------------------------------------------------------------- F7


class TestExitLegValidationF7:
    async def test_exit_on_entry_side_is_rejected(
        self, gateway: KrakenGateway, rest: _Rest
    ) -> None:
        same_side = _leg(LegRole.STOP_LOSS, OrderType.STOP_LOSS, TradeType.BUY, trigger="49000")
        with pytest.raises(ValueError, match="opposite"):
            await gateway.place_bracket(_PAIR, _spec(same_side))
        assert rest.calls == []

    async def test_post_only_exit_is_not_silently_dropped(
        self, gateway: KrakenGateway, rest: _Rest
    ) -> None:
        maker = _leg(LegRole.TAKE_PROFIT, OrderType.LIMIT_MAKER, TradeType.SELL, price="52000")
        with pytest.raises(UnsupportedOrderError):
            await gateway.place_bracket(_PAIR, _spec(maker))
        assert rest.calls == []


# --------------------------------------------------------------------------- F8


def _pct(fraction: str) -> TriggerSpec:
    return TriggerSpec(kind=TriggerKind.PERCENT, value=_D(fraction))


class TestResolveTriggerF8:
    async def test_stop_loss_limit_trigger_without_limit_price_is_a_clear_error(
        self, gateway: KrakenGateway, rest: _Rest
    ) -> None:
        trigger = TriggerSpec(kind=TriggerKind.ABSOLUTE, value=_D("49000"))
        with pytest.raises(ValueError, match="limit price"):
            await gateway.place_order(
                _PAIR, OrderType.STOP_LOSS_LIMIT, TradeType.SELL, _D("0.5"), trigger=trigger
            )
        assert rest.calls == []

    @pytest.mark.parametrize(
        "order_type", [OrderType.LIMIT, OrderType.MARKET, OrderType.LIMIT_MAKER]
    )
    async def test_trigger_on_non_conditional_type_is_rejected(
        self, gateway: KrakenGateway, rest: _Rest, order_type: OrderType
    ) -> None:
        trigger = TriggerSpec(kind=TriggerKind.ABSOLUTE, value=_D("49000"))
        with pytest.raises(ValueError, match="conditional"):
            await gateway.place_order(
                _PAIR, order_type, TradeType.SELL, _D("0.5"), _D("50000"), trigger=trigger
            )
        assert rest.calls == []

    @pytest.mark.parametrize(
        ("fraction", "expected"),
        [("0.0000001", "#0.00001%"), ("0.9999", "#99.99%"), ("0.02", "#2%")],
    )
    async def test_percent_points_have_no_exponent(
        self, gateway: KrakenGateway, rest: _Rest, fraction: str, expected: str
    ) -> None:
        await gateway.place_order(
            _PAIR, OrderType.STOP_LOSS, TradeType.SELL, _D("0.5"), trigger=_pct(fraction)
        )
        body = next(b for n, b in rest.calls if n == "add_order")
        assert body["price"] == expected


# --------------------------------------------------------------------------- F9


def _fake_gateway(linked: frozenset[LinkedMode], *, marked: bool) -> object:
    async def place_oco(self: object, target: str, spec: object, *, options: Any = None) -> Any:
        raise UnsupportedOrderError(exchange="fake", feature="oco")

    class _Fake:
        order_capabilities = OrderCapabilities(
            order_types=frozenset(),
            trigger_kinds=frozenset(),
            trigger_references=frozenset(),
            supports_trailing=False,
            linked=linked,
            native_linked=False,
        )

        async def cancel_linked(self, handle: object) -> Any:
            raise NotImplementedError

    _Fake.place_oco = unsupported_linked(place_oco) if marked else place_oco  # type: ignore[attr-defined]
    return _Fake()


class TestUnsupportedMarkerContractF9:
    def test_marked_stub_that_advertises_the_mode_is_a_failure(self) -> None:
        gateway = _fake_gateway(frozenset({LinkedMode.OCO}), marked=True)
        failures = check_linked_protocol_capabilities(gateway)
        assert len(failures) == 1
        assert "OCO" in failures[0]

    def test_marked_stub_that_does_not_advertise_the_mode_passes(self) -> None:
        gateway = _fake_gateway(frozenset(), marked=True)
        assert check_linked_protocol_capabilities(gateway) == []
