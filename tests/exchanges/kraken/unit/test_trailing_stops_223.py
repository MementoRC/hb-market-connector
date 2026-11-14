"""Kraken trailing orders (issue #223): TRAILING_STOP / TRAILING_STOP_LIMIT.

Contract (Kraken AddOrder):
- ``price`` is the trail distance (``price`` argument), always sent relative as ``+<d>``
  or ``+<d>%`` when ``price_in_percent``; never ``#`` or ``-``.
- ``price2`` (``limit_price`` argument) is a SIGNED offset from the trigger, sent as
  ``+<o>`` / ``-<o>`` (zero -> ``+0``), with ``%`` appended when ``price_in_percent``.
- Decimals are sent in plain notation (no exponent). Validation precedes any HTTP call.

Only the REST transport is faked; the bridge -> converter -> mixin path runs for real.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from data_type_primitives.common import OrderType

from market_connector.exchanges.kraken.hb_compat.kraken_bridge import KrakenConnectorBridge
from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway
from market_connector.primitives import TradeType
from market_connector.transport.response import Response

_ADD_ORDER_OK = {
    "error": [],
    "result": {"descr": {"order": "x"}, "txid": ["OTRAIL-XXXXX-YYYYYY"]},
}


class _RecordingRest:
    """Fake REST transport recording every request's endpoint and body."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def request(
        self, endpoint_name: str, data: dict[str, Any] | None = None, **_: Any
    ) -> Any:
        self.calls.append((endpoint_name, dict(data or {})))
        return Response(
            raw=_ADD_ORDER_OK,
            status_code=200,
            headers=httpx.Headers(),
            _endpoint=endpoint_name,
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


@pytest.fixture
def bridge(gateway: KrakenGateway) -> KrakenConnectorBridge:
    b = KrakenConnectorBridge(api_key="k", secret_key="c2VjcmV0")
    b._gateway = gateway
    return b


_PAIR = "XBTUSD"
_AMOUNT = Decimal("0.5")
_SIDES = [
    pytest.param(TradeType.BUY, "buy", id="buy"),
    pytest.param(TradeType.SELL, "sell", id="sell"),
]
# (offset, percent, expected price2)
_OFFSETS = [
    pytest.param(Decimal("50"), False, "+50", id="positive"),
    pytest.param(Decimal("-50.5"), False, "-50.5", id="negative"),
    pytest.param(Decimal(0), False, "+0", id="zero"),
    pytest.param(Decimal("0.5"), True, "+0.5%", id="positive_percent"),
    pytest.param(Decimal("-0.5"), True, "-0.5%", id="negative_percent"),
    pytest.param(Decimal(0), True, "+0%", id="zero_percent"),
]


@pytest.fixture(params=["gateway", "bridge"])
def venue(request: pytest.FixtureRequest) -> Any:
    """The same behaviour is required through the gateway and the bridge."""
    return request.getfixturevalue(request.param)


# ---------------------------------------------------------------------------
# TRAILING_STOP
# ---------------------------------------------------------------------------


class TestTrailingStop:
    @pytest.mark.parametrize(("side", "wire_side"), _SIDES)
    async def test_absolute_distance(
        self, venue: Any, rest: _RecordingRest, side: TradeType, wire_side: str
    ) -> None:
        txid = await venue.place_order(
            _PAIR, OrderType.TRAILING_STOP, side, _AMOUNT, Decimal("150.5")
        )
        assert txid == "OTRAIL-XXXXX-YYYYYY"
        (body,) = rest.bodies
        assert body["ordertype"] == "trailing-stop"
        assert body["price"] == "+150.5"
        assert "price2" not in body
        assert body["pair"] == _PAIR
        assert body["type"] == wire_side
        assert body["volume"] == "0.5"

    @pytest.mark.parametrize(("side", "wire_side"), _SIDES)
    async def test_percent_distance(
        self, venue: Any, rest: _RecordingRest, side: TradeType, wire_side: str
    ) -> None:
        await venue.place_order(
            _PAIR,
            OrderType.TRAILING_STOP,
            side,
            _AMOUNT,
            Decimal("2.5"),
            price_in_percent=True,
        )
        (body,) = rest.bodies
        assert body["ordertype"] == "trailing-stop"
        assert body["price"] == "+2.5%"
        assert "price2" not in body
        assert body["type"] == wire_side

    @pytest.mark.parametrize(
        ("distance", "percent", "expected"),
        [
            pytest.param(Decimal("5E+2"), False, "+500", id="exponent_large"),
            pytest.param(Decimal("1E-7"), False, "+0.0000001", id="exponent_small"),
            pytest.param(Decimal("1E-3"), True, "+0.001%", id="exponent_small_percent"),
        ],
    )
    async def test_plain_decimal_notation(
        self,
        venue: Any,
        rest: _RecordingRest,
        distance: Decimal,
        percent: bool,
        expected: str,
    ) -> None:
        await venue.place_order(
            _PAIR,
            OrderType.TRAILING_STOP,
            TradeType.SELL,
            _AMOUNT,
            distance,
            price_in_percent=percent,
        )
        assert rest.bodies[0]["price"] == expected

    async def test_no_oflags_sent(self, venue: Any, rest: _RecordingRest) -> None:
        await venue.place_order(
            _PAIR, OrderType.TRAILING_STOP, TradeType.SELL, _AMOUNT, Decimal("100")
        )
        assert "oflags" not in rest.bodies[0]


# ---------------------------------------------------------------------------
# TRAILING_STOP_LIMIT
# ---------------------------------------------------------------------------


class TestTrailingStopLimit:
    @pytest.mark.parametrize(("side", "wire_side"), _SIDES)
    @pytest.mark.parametrize(("offset", "offset_percent", "expected_price2"), _OFFSETS)
    async def test_distance_and_signed_offset(
        self,
        venue: Any,
        rest: _RecordingRest,
        side: TradeType,
        wire_side: str,
        offset: Decimal,
        offset_percent: bool,
        expected_price2: str,
    ) -> None:
        # price_in_percent applies to both fields, so the distance follows the flag.
        distance = Decimal("2.5") if offset_percent else Decimal("150")
        await venue.place_order(
            _PAIR,
            OrderType.TRAILING_STOP_LIMIT,
            side,
            _AMOUNT,
            distance,
            limit_price=offset,
            price_in_percent=offset_percent,
        )
        (body,) = rest.bodies
        assert body["ordertype"] == "trailing-stop-limit"
        assert body["price"] == ("+2.5%" if offset_percent else "+150")
        assert body["price2"] == expected_price2
        assert body["type"] == wire_side
        assert body["pair"] == _PAIR
        assert body["volume"] == "0.5"

    async def test_plain_decimal_notation(self, venue: Any, rest: _RecordingRest) -> None:
        await venue.place_order(
            _PAIR,
            OrderType.TRAILING_STOP_LIMIT,
            TradeType.SELL,
            _AMOUNT,
            Decimal("5E+2"),
            limit_price=Decimal("-1E-7"),
        )
        (body,) = rest.bodies
        assert body["price"] == "+500"
        assert body["price2"] == "-0.0000001"

    async def test_no_oflags_sent(self, venue: Any, rest: _RecordingRest) -> None:
        await venue.place_order(
            _PAIR,
            OrderType.TRAILING_STOP_LIMIT,
            TradeType.SELL,
            _AMOUNT,
            Decimal("100"),
            limit_price=Decimal("-5"),
        )
        assert "oflags" not in rest.bodies[0]


# ---------------------------------------------------------------------------
# Validation: ValueError before any HTTP call
# ---------------------------------------------------------------------------

_D = Decimal("100")
_O = Decimal("-5")
_BAD_ARGS = [
    pytest.param(OrderType.TRAILING_STOP, None, None, False, id="ts_no_distance"),
    pytest.param(OrderType.TRAILING_STOP, Decimal(0), None, False, id="ts_zero_distance"),
    pytest.param(OrderType.TRAILING_STOP, Decimal(-1), None, False, id="ts_negative_distance"),
    pytest.param(OrderType.TRAILING_STOP, None, None, True, id="ts_no_distance_percent"),
    pytest.param(OrderType.TRAILING_STOP, Decimal(0), None, True, id="ts_zero_distance_percent"),
    pytest.param(OrderType.TRAILING_STOP, _D, _O, False, id="ts_with_limit_offset"),
    pytest.param(OrderType.TRAILING_STOP_LIMIT, None, _O, False, id="tsl_no_distance"),
    pytest.param(OrderType.TRAILING_STOP_LIMIT, Decimal(0), _O, False, id="tsl_zero_distance"),
    pytest.param(OrderType.TRAILING_STOP_LIMIT, Decimal(-1), _O, False, id="tsl_negative_distance"),
    pytest.param(OrderType.TRAILING_STOP_LIMIT, _D, None, False, id="tsl_no_limit_offset"),
    pytest.param(OrderType.TRAILING_STOP_LIMIT, _D, None, True, id="tsl_no_limit_offset_percent"),
]


class TestTrailingValidation:
    @pytest.mark.parametrize(("order_type", "price", "limit_price", "percent"), _BAD_ARGS)
    async def test_rejects_before_http(
        self,
        venue: Any,
        rest: _RecordingRest,
        order_type: Any,
        price: Decimal | None,
        limit_price: Decimal | None,
        percent: bool,
    ) -> None:
        with pytest.raises(ValueError):  # noqa: PT011
            await venue.place_order(
                _PAIR,
                order_type,
                TradeType.SELL,
                _AMOUNT,
                price,
                limit_price=limit_price,
                price_in_percent=percent,
            )
        assert rest.calls == []


# ---------------------------------------------------------------------------
# Regression: #212 conditional types unchanged
# ---------------------------------------------------------------------------


class TestConditionalRegression:
    @pytest.mark.parametrize(
        ("order_type", "expected"),
        [
            pytest.param(OrderType.STOP_LOSS, "stop-loss", id="stop_loss"),
            pytest.param(OrderType.TAKE_PROFIT, "take-profit", id="take_profit"),
        ],
    )
    async def test_trigger_only_unchanged(
        self, venue: Any, rest: _RecordingRest, order_type: Any, expected: str
    ) -> None:
        await venue.place_order(_PAIR, order_type, TradeType.SELL, _AMOUNT, Decimal("27000.5"))
        (body,) = rest.bodies
        assert body["ordertype"] == expected
        assert body["price"] == "27000.5"
        assert "price2" not in body

    @pytest.mark.parametrize(
        ("order_type", "expected"),
        [
            pytest.param(OrderType.STOP_LOSS_LIMIT, "stop-loss-limit", id="stop_loss_limit"),
            pytest.param(OrderType.TAKE_PROFIT_LIMIT, "take-profit-limit", id="take_profit_limit"),
        ],
    )
    async def test_trigger_plus_limit_unchanged(
        self, venue: Any, rest: _RecordingRest, order_type: Any, expected: str
    ) -> None:
        await venue.place_order(
            _PAIR,
            order_type,
            TradeType.SELL,
            _AMOUNT,
            Decimal("2.5"),
            limit_price=Decimal("26950"),
            price_in_percent=True,
        )
        (body,) = rest.bodies
        assert body["ordertype"] == expected
        assert body["price"] == "#2.5%"
        assert body["price2"] == "26950"
