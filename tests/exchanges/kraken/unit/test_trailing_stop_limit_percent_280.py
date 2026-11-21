"""RED acceptance tests for issue #280 (Kraken percent trailing-stop-limit, effective levels).

A. A percent ``TRAILING_STOP_LIMIT`` via ``trigger=TriggerSpec`` is accepted. When
   ``trigger.trail.kind`` is PERCENT the ``limit_price`` kwarg is a signed fraction of the trigger
   level (0.02 = 2%): ``price`` = ``"+2%"``, ``price2`` = ``"-0.5%"``; plain decimal notation, no
   ``oflags``. The absolute path and the legacy ``price_in_percent`` form are unchanged.
B. ``OrderInfo`` (REST), ``OpenOrderDetail`` (WS openOrders) and ``LegSnapshot`` expose
   ``trigger_level`` / ``limit_level`` (``Decimal | None``): when ``misc`` contains ``stopped``
   they are the absolute ``descr.price`` / ``descr.price2``, otherwise ``None``. Never derived
   from ``stopprice`` / ``limitprice``. Fixtures are the live #230 order O2HO5G-AVEKE-IKFLLI.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from data_type_primitives import OrderState, TrailSpec, TriggerKind, TriggerSpec
from data_type_primitives.common import OrderType

from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway
from market_connector.exchanges.kraken.schemas.rest import OrderInfo
from market_connector.exchanges.kraken.schemas.ws import OpenOrdersEvent
from market_connector.primitives import TradeType
from market_connector.protocols import LegSnapshot
from market_connector.transport.response import Response

_D = Decimal
_FIXTURES = Path(__file__).parent.parent / "fixtures"
_PAIR = "XBTUSD"
_TXID = "O2HO5G-AVEKE-IKFLLI"
_USERREF = -17
_ADD_ORDER_OK = {"error": [], "result": {"descr": {"order": "x"}, "txid": ["OABC-XXXXX-YYYYYY"]}}


def _load(*parts: str) -> Any:
    with _FIXTURES.joinpath(*parts).open() as fh:
        return json.load(fh)


_REST_230 = _load("rest", "query_orders_trailing_stop_limit_230.json")
_PRE: dict[str, Any] = _REST_230["pre_trigger"]
_POST: dict[str, Any] = _REST_230["post_trigger"]
_PLAIN_WS_FRAME = _load("ws", "open_orders.json")
_PLAIN_ID = "OGTT3Y-C6I3P-XRI6HX"


# ---------------------------------------------------------------------------
# A. Encoder
# ---------------------------------------------------------------------------


class _RecordingRest:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def request(
        self, endpoint_name: str, data: dict[str, Any] | None = None, **_: Any
    ) -> Response:
        self.calls.append((endpoint_name, dict(data or {})))
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


def _trail(kind: TriggerKind, distance: str) -> TriggerSpec:
    return TriggerSpec(
        kind=TriggerKind.ABSOLUTE,
        value=_D("1"),  # placeholder, ignored for trailing types
        trail=TrailSpec(kind=kind, distance=_D(distance)),
    )


class TestPercentTrailingStopLimitPayload:
    @pytest.mark.parametrize(
        ("limit_fraction", "price", "price2"),
        [
            ("-0.005", "+2%", "-0.5%"),
            ("0.01", "+2%", "+1%"),
        ],
        ids=["negative-offset", "positive-offset"],
    )
    async def test_percent_trail_with_fractional_limit_offset(
        self,
        gateway: KrakenGateway,
        rest: _RecordingRest,
        limit_fraction: str,
        price: str,
        price2: str,
    ) -> None:
        await gateway.place_order(
            _PAIR,
            OrderType.TRAILING_STOP_LIMIT,
            TradeType.BUY,
            _D("0.5"),
            None,
            limit_price=_D(limit_fraction),
            trigger=_trail(TriggerKind.PERCENT, "0.02"),
        )
        assert rest.bodies == [
            {
                "pair": _PAIR,
                "type": "buy",
                "volume": "0.5",
                "ordertype": "trailing-stop-limit",
                "price": price,
                "price2": price2,
            }
        ]

    async def test_payload_uses_plain_decimal_notation_and_no_oflags(
        self, gateway: KrakenGateway, rest: _RecordingRest
    ) -> None:
        await gateway.place_order(
            _PAIR,
            OrderType.TRAILING_STOP_LIMIT,
            TradeType.SELL,
            _D("0.5"),
            None,
            limit_price=_D("-0.0000005"),
            trigger=_trail(TriggerKind.PERCENT, "1E-3"),
        )
        (body,) = rest.bodies
        assert "oflags" not in body
        assert "e" not in body["price"].lower()
        assert "e" not in body["price2"].lower()
        assert body["price"] == "+0.1%"
        assert body["price2"] == "-0.00005%"

    async def test_absolute_trail_path_unchanged(
        self, gateway: KrakenGateway, rest: _RecordingRest
    ) -> None:
        await gateway.place_order(
            _PAIR,
            OrderType.TRAILING_STOP_LIMIT,
            TradeType.BUY,
            _D("0.5"),
            None,
            limit_price=_D("-5"),
            trigger=_trail(TriggerKind.RELATIVE_OFFSET, "150.5"),
        )
        assert rest.bodies == [
            {
                "pair": _PAIR,
                "type": "buy",
                "volume": "0.5",
                "ordertype": "trailing-stop-limit",
                "price": "+150.5",
                "price2": "-5",
            }
        ]


# ---------------------------------------------------------------------------
# B. Effective levels
# ---------------------------------------------------------------------------


class TestRestOrderInfoLevels:
    def test_pre_trigger_levels_are_none(self) -> None:
        info = OrderInfo.model_validate(_PRE)
        assert info.trigger_level is None
        assert info.limit_level is None

    def test_post_trigger_levels_are_descr_prices(self) -> None:
        info = OrderInfo.model_validate(_POST)
        assert info.trigger_level == _D("62520.8")
        assert info.limit_level == _D("62620.8")
        assert isinstance(info.trigger_level, Decimal)
        assert isinstance(info.limit_level, Decimal)

    def test_post_trigger_levels_differ_from_stopprice_and_limitprice(self) -> None:
        info = OrderInfo.model_validate(_POST)
        assert info.trigger_level != _D(_POST["stopprice"])
        assert info.limit_level != _D(_POST["limitprice"])

    def test_misc_is_a_comma_separated_flag_list(self) -> None:
        info = OrderInfo.model_validate({**_POST, "misc": "partial,stopped"})
        assert info.trigger_level == _D("62520.8")
        assert info.limit_level == _D("62620.8")

    def test_plain_limit_order_levels_are_none(self) -> None:
        info = OrderInfo.model_validate(
            {
                "status": "open",
                "misc": "",
                "descr": {
                    "pair": _PAIR,
                    "type": "buy",
                    "ordertype": "limit",
                    "price": "50000",
                    "price2": "0",
                },
                "vol": "1.0",
                "vol_exec": "0",
            }
        )
        assert info.trigger_level is None
        assert info.limit_level is None


class TestWsOpenOrderDetailLevels:
    def test_pre_trigger_frame_levels_are_none(self) -> None:
        raw = _load("ws", "open_orders_trailing_stop_limit_230_pre.json")
        order = OpenOrdersEvent.from_raw_frame(raw).orders[_TXID]
        assert order.status == "open"
        assert order.trigger_level is None
        assert order.limit_level is None

    def test_post_trigger_frame_levels_are_descr_prices(self) -> None:
        raw = _load("ws", "open_orders_trailing_stop_limit_230_post.json")
        order = OpenOrdersEvent.from_raw_frame(raw).orders[_TXID]
        assert order.status == "closed"
        assert order.trigger_level == _D("62520.8")
        assert order.limit_level == _D("62620.8")
        assert order.trigger_level != _D("62522.9")  # stopprice
        assert order.limit_level != _D("62470.8")  # limitprice

    def test_plain_limit_frame_levels_are_none(self) -> None:
        order = OpenOrdersEvent.from_raw_frame(_PLAIN_WS_FRAME).orders[_PLAIN_ID]
        assert order.trigger_level is None
        assert order.limit_level is None


class _FakeRest:
    """Serves the #230 order as open (pre-trigger) or closed (post-trigger)."""

    def __init__(self, order: dict[str, Any]) -> None:
        self._order = order

    async def request(
        self, endpoint_name: str, data: dict[str, Any] | None = None, **_: Any
    ) -> Response:
        body = dict(data or {})
        is_open = self._order["status"] == "open"
        matches = "userref" not in body or str(body["userref"]) == str(self._order["userref"])
        if endpoint_name == "open_orders":
            result: dict[str, Any] = {"open": {_TXID: self._order} if is_open and matches else {}}
        elif endpoint_name == "closed_orders":
            closed = {_TXID: self._order} if not is_open and matches else {}
            result = {"closed": closed, "count": len(closed)}
        elif endpoint_name == "query_orders":
            result = {t: self._order for t in body["txid"].split(",") if t == _TXID}
        else:
            raise AssertionError(f"unexpected endpoint {endpoint_name}")
        return Response(
            raw={"error": [], "result": result},
            status_code=200,
            headers=httpx.Headers(),
            _endpoint=endpoint_name,
        )


def _gateway_serving(order: dict[str, Any]) -> KrakenGateway:
    gw = KrakenGateway(api_key="k", secret_key="c2VjcmV0")
    gw._rest = _FakeRest(order)  # type: ignore[assignment]
    gw._started = True
    return gw


class TestLegSnapshotLevels:
    async def test_pre_trigger_leg_levels_are_none(self) -> None:
        snap = await _gateway_serving(_PRE).get_leg(_PAIR, link_tag=str(_USERREF))
        assert snap is not None
        assert snap.state is OrderState.OPEN
        assert snap.trigger_level is None
        assert snap.limit_level is None

    async def test_post_trigger_leg_levels_are_descr_prices(self) -> None:
        snap = await _gateway_serving(_POST).get_leg(_PAIR, link_tag=str(_USERREF))
        assert snap is not None
        assert snap.trigger_level == _D("62520.8")
        assert snap.limit_level == _D("62620.8")

    async def test_open_legs_listing_carries_levels_field(self) -> None:
        legs = await _gateway_serving(_PRE).list_open_legs(_PAIR)
        assert len(legs) == 1
        assert legs[0].trigger_level is None
        assert legs[0].limit_level is None

    def test_constructor_without_new_fields_defaults_to_none(self) -> None:
        snap = LegSnapshot(client_order_id="1", link_tag="1", state=OrderState.OPEN, filled=_D(0))
        assert snap.trigger_level is None
        assert snap.limit_level is None

    def test_constructor_accepts_levels(self) -> None:
        snap = LegSnapshot(
            client_order_id="1",
            link_tag="1",
            state=OrderState.FILLED,
            filled=_D(1),
            trigger_level=_D("62520.8"),
            limit_level=_D("62620.8"),
        )
        assert snap.trigger_level == _D("62520.8")
        assert snap.limit_level == _D("62620.8")


# ---------------------------------------------------------------------------
# C. limit_level only for order types that have a limit leg
# ---------------------------------------------------------------------------

_NO_LIMIT_CASES = [
    pytest.param("trailing-stop", "62520.8", "0.00000", id="trailing-stop-zero5"),
    pytest.param("stop-loss", "60000.0", "0", id="stop-loss-zero"),
    pytest.param("take-profit", "70000.0", "0", id="take-profit-zero"),
    pytest.param("stop-loss", "60000.0", None, id="stop-loss-absent-price2"),
]


def _stopped_order(ordertype: str, price: str, price2: str | None) -> dict[str, Any]:
    descr: dict[str, Any] = {**_POST["descr"], "ordertype": ordertype, "price": price}
    if price2 is None:
        descr.pop("price2")
    else:
        descr["price2"] = price2
    return {**_POST, "descr": descr, "misc": "stopped"}


def _ws_frame(order: dict[str, Any]) -> list[Any]:
    return [[{_TXID: order}], "openOrders", {"sequence": 1}]


class TestLimitLevelOnlyForLimitTypes:
    @pytest.mark.parametrize(("ordertype", "price", "price2"), _NO_LIMIT_CASES)
    def test_rest_limit_level_none_without_limit_leg(
        self, ordertype: str, price: str, price2: str | None
    ) -> None:
        info = OrderInfo.model_validate(_stopped_order(ordertype, price, price2))
        assert info.trigger_level == _D(price)
        assert info.limit_level is None

    @pytest.mark.parametrize(("ordertype", "price", "price2"), _NO_LIMIT_CASES)
    def test_ws_limit_level_none_without_limit_leg(
        self, ordertype: str, price: str, price2: str | None
    ) -> None:
        raw = _ws_frame(_stopped_order(ordertype, price, price2))
        order = OpenOrdersEvent.from_raw_frame(raw).orders[_TXID]
        assert order.trigger_level == _D(price)
        assert order.limit_level is None

    @pytest.mark.parametrize(("ordertype", "price", "price2"), _NO_LIMIT_CASES)
    async def test_leg_limit_level_none_without_limit_leg(
        self, ordertype: str, price: str, price2: str | None
    ) -> None:
        order = _stopped_order(ordertype, price, price2)
        snap = await _gateway_serving(order).get_leg(_PAIR, link_tag=str(_USERREF))
        assert snap is not None
        assert snap.trigger_level == _D(price)
        assert snap.limit_level is None

    def test_rest_stop_loss_limit_keeps_limit_level(self) -> None:
        info = OrderInfo.model_validate(_stopped_order("stop-loss-limit", "60000.0", "59900.0"))
        assert info.trigger_level == _D("60000.0")
        assert info.limit_level == _D("59900.0")

    def test_ws_stop_loss_limit_keeps_limit_level(self) -> None:
        raw = _ws_frame(_stopped_order("stop-loss-limit", "60000.0", "59900.0"))
        order = OpenOrdersEvent.from_raw_frame(raw).orders[_TXID]
        assert order.limit_level == _D("59900.0")

    async def test_leg_stop_loss_limit_keeps_limit_level(self) -> None:
        order = _stopped_order("stop-loss-limit", "60000.0", "59900.0")
        snap = await _gateway_serving(order).get_leg(_PAIR, link_tag=str(_USERREF))
        assert snap is not None
        assert snap.limit_level == _D("59900.0")
