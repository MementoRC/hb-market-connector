"""RED acceptance tests for issue #240 (criteria 2 and 4): Kraken bracket and OCO.

Contract pinned here (none of it exists yet):

* ``KrakenGateway.place_bracket(target, spec: LinkedOrderSpec, *, options=None)`` sends ONE
  AddOrder for the entry carrying the single exit as ``close[ordertype]`` / ``close[price]``
  / ``close[price2]``. No second AddOrder, no cancel: no client-side leg management.
* The entry carries an int32 ``userref`` produced by ``KrakenUserrefLinkCodec`` (leg 0); the
  returned ``BracketHandle`` has ``emulated is False``, ``entry_client_id`` equal to that
  userref and ``group_id`` equal to the decoded group.
* A lost AddOrder reply raises ``OrderOutcomeUnknownError`` whose ``client_order_id`` is the
  entry userref, so the order can be found with ``get_open_orders(userref=...)``.
* ``place_oco`` raises ``UnsupportedOrderError(exchange="kraken", feature="oco")`` before
  any request; capabilities advertise ``LinkedMode.BRACKET`` only, with ``native_linked``.

Kraken allows one conditional close per order, so only single-exit brackets are pinned.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from data_type_primitives import (
    LegRole,
    LegSpec,
    LinkedOrderSpec,
    LinkKind,
    TriggerKind,
    TriggerSpec,
)
from data_type_primitives.common import OrderType, TradeType

from market_connector.capabilities import LinkedMode
from market_connector.exceptions import OrderOutcomeUnknownError, UnsupportedOrderError
from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway
from market_connector.protocols import BracketHandle, SupportsBracket
from market_connector.transport.response import Response

_ADD_ORDER_OK = {"error": [], "result": {"descr": {"order": "x"}, "txid": ["OENTRY-XXXXX-YYYYYY"]}}
_PAIR = "XBTUSD"
_D = Decimal
_INT32_MAX = 2**31 - 1


class _RecordingRest:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.exc: Exception | None = None

    async def request(
        self, endpoint_name: str, data: dict[str, Any] | None = None, **_: Any
    ) -> Response:
        self.calls.append((endpoint_name, dict(data or {})))
        if self.exc is not None:
            raise self.exc
        return Response(
            raw=_ADD_ORDER_OK, status_code=200, headers=httpx.Headers(), _endpoint=endpoint_name
        )

    @property
    def bodies(self) -> list[dict[str, Any]]:
        return [body for name, body in self.calls if name == "add_order"]


@pytest.fixture
def rest() -> _RecordingRest:
    return _RecordingRest()


@pytest.fixture
def gateway(rest: _RecordingRest) -> KrakenGateway:
    gw = KrakenGateway(api_key="k", secret_key="c2VjcmV0")
    gw._rest = rest  # type: ignore[assignment]
    gw._started = True
    return gw


def _entry() -> LegSpec:
    return LegSpec(
        role=LegRole.ENTRY,
        order_type=OrderType.LIMIT,
        trade_type=TradeType.BUY,
        amount=_D("0.5"),
        price=_D("50000"),
    )


def _exit(
    role: LegRole,
    order_type: OrderType,
    *,
    price: str | None = None,
    trigger: str | None = None,
) -> LegSpec:
    return LegSpec(
        role=role,
        order_type=order_type,
        trade_type=TradeType.SELL,
        amount=_D("0.5"),
        price=None if price is None else _D(price),
        trigger=None
        if trigger is None
        else TriggerSpec(kind=TriggerKind.ABSOLUTE, value=_D(trigger)),
    )


def _bracket(exit_leg: LegSpec) -> LinkedOrderSpec:
    return LinkedOrderSpec(kind=LinkKind.BRACKET, legs=(_entry(), exit_leg))


# id -> (exit leg, expected close[...] fields)
_EXITS: dict[str, tuple[LegSpec, dict[str, str]]] = {
    "stop-loss": (
        _exit(LegRole.STOP_LOSS, OrderType.STOP_LOSS, trigger="49000"),
        {"close[ordertype]": "stop-loss", "close[price]": "49000"},
    ),
    "stop-loss-limit": (
        _exit(LegRole.STOP_LOSS, OrderType.STOP_LOSS_LIMIT, price="48900", trigger="49000"),
        {"close[ordertype]": "stop-loss-limit", "close[price]": "49000", "close[price2]": "48900"},
    ),
    "take-profit": (
        _exit(LegRole.TAKE_PROFIT, OrderType.TAKE_PROFIT, trigger="52000"),
        {"close[ordertype]": "take-profit", "close[price]": "52000"},
    ),
    "take-profit-limit-order": (
        _exit(LegRole.TAKE_PROFIT, OrderType.LIMIT, price="52000"),
        {"close[ordertype]": "limit", "close[price]": "52000"},
    ),
}
_PARAMS = [pytest.param(k, id=k) for k in _EXITS]


def _oco_spec() -> LinkedOrderSpec:
    legs = (
        LegSpec(
            role=LegRole.OCO_LEG,
            order_type=OrderType.LIMIT,
            trade_type=TradeType.SELL,
            amount=_D("0.5"),
            price=_D("52000"),
        ),
        LegSpec(
            role=LegRole.OCO_LEG,
            order_type=OrderType.STOP_LOSS,
            trade_type=TradeType.SELL,
            amount=_D("0.5"),
            trigger=TriggerSpec(kind=TriggerKind.ABSOLUTE, value=_D("49000")),
        ),
    )
    return LinkedOrderSpec(kind=LinkKind.OCO, legs=legs)


@pytest.mark.parametrize("case", _PARAMS)
class TestBracketViaConditionalClose:
    async def test_single_add_order_with_close_fields(
        self, gateway: KrakenGateway, rest: _RecordingRest, case: str
    ) -> None:
        exit_leg, close_fields = _EXITS[case]

        await gateway.place_bracket(_PAIR, _bracket(exit_leg))

        assert len(rest.calls) == 1, "bracket must be exactly one AddOrder, no leg management"
        body = rest.bodies[0]
        assert {k: v for k, v in body.items() if k.startswith("close[")} == close_fields
        assert body["ordertype"] == "limit"
        assert body["type"] == "buy"
        assert body["volume"] == "0.5"
        assert body["price"] == "50000"
        assert body["pair"] == _PAIR

    async def test_entry_carries_codec_userref_and_handle_agrees(
        self, gateway: KrakenGateway, rest: _RecordingRest, case: str
    ) -> None:
        handle = await gateway.place_bracket(_PAIR, _bracket(_EXITS[case][0]))

        userref = rest.bodies[0]["userref"]
        assert isinstance(userref, int)
        assert -_INT32_MAX - 1 <= userref <= -1  # codec userrefs are negative int32
        from market_connector.exchanges.kraken.linked_order_codec import (  # noqa: PLC0415
            KrakenUserrefLinkCodec,
        )

        decoded = KrakenUserrefLinkCodec().decode(userref)
        assert decoded is not None
        group_id, leg_index = decoded
        assert leg_index == 0  # the entry is ordered_legs()[0]
        assert isinstance(handle, BracketHandle)
        assert handle.emulated is False
        assert handle.entry_client_id == userref
        assert handle.group_id == group_id

    async def test_no_cl_ord_id_is_sent(
        self, gateway: KrakenGateway, rest: _RecordingRest, case: str
    ) -> None:
        """cl_ord_id and userref are mutually exclusive on AddOrder; the bracket uses userref."""
        await gateway.place_bracket(_PAIR, _bracket(_EXITS[case][0]))
        assert "cl_ord_id" not in rest.bodies[0]


class TestBracketOutcome:
    async def test_distinct_brackets_get_distinct_groups(
        self, gateway: KrakenGateway, rest: _RecordingRest
    ) -> None:
        spec = _bracket(_EXITS["stop-loss"][0])
        first = await gateway.place_bracket(_PAIR, spec)
        second = await gateway.place_bracket(_PAIR, spec)
        assert first.group_id != second.group_id
        assert rest.bodies[0]["userref"] != rest.bodies[1]["userref"]

    async def test_lost_reply_raises_unknown_with_entry_userref(
        self, gateway: KrakenGateway, rest: _RecordingRest
    ) -> None:
        rest.exc = httpx.ReadTimeout("response lost")

        with pytest.raises(OrderOutcomeUnknownError) as info:
            await gateway.place_bracket(_PAIR, _bracket(_EXITS["stop-loss"][0]))

        userref = rest.bodies[0]["userref"]
        assert str(info.value.client_order_id) == str(userref)
        assert len(rest.bodies) == 1  # never re-sent


class TestOcoUnsupported:
    async def test_place_oco_raises_unsupported(
        self, gateway: KrakenGateway, rest: _RecordingRest
    ) -> None:
        with pytest.raises(UnsupportedOrderError) as info:
            await gateway.place_oco(_PAIR, _oco_spec())

        assert info.value.exchange == "kraken"
        assert info.value.feature == "oco"
        assert rest.calls == []


class TestCapabilities:
    def test_advertises_bracket_not_oco(self, gateway: KrakenGateway) -> None:
        caps = gateway.order_capabilities
        assert LinkedMode.BRACKET in caps.linked
        assert LinkedMode.OCO not in caps.linked

    def test_bracket_is_native(self, gateway: KrakenGateway) -> None:
        assert gateway.order_capabilities.native_linked is True

    def test_gateway_is_supports_bracket(self, gateway: KrakenGateway) -> None:
        assert isinstance(gateway, SupportsBracket)
