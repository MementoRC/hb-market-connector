"""RED acceptance tests for issue #299: Kraken rejects a percent trail distance below 0.1%.

Live (2026-11-21, validate-only): percent trail distance < 0.1% is rejected by Kraken
(``EGeneral:Invalid arguments:price``) for trailing-stop and trailing-stop-limit; 0.1% and above
is accepted. ``price2`` (limit offset) has no minimum.

Contract: a PERCENT trail distance below 0.1% raises ``UnsupportedOrderError(feature=
"percent_trail_min")`` before any HTTP request, via ``trigger=TriggerSpec`` (fraction) and via the
legacy ``price_in_percent`` form (percent points). Absolute trails and ``price2`` are unaffected.
Runs through the gateway and the hb_compat bridge.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from data_type_primitives import TrailSpec, TriggerKind, TriggerSpec
from data_type_primitives.common import OrderType

from market_connector.exceptions import UnsupportedOrderError
from market_connector.exchanges.kraken.hb_compat.kraken_bridge import KrakenConnectorBridge
from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway
from market_connector.primitives import TradeType
from market_connector.transport.response import Response

_D = Decimal
_PAIR = "XBTUSD"
_AMOUNT = _D("0.5")
_ADD_ORDER_OK = {"error": [], "result": {"descr": {"order": "x"}, "txid": ["OPCT-XXXXX-YYYYYY"]}}


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


@pytest.fixture
def bridge(gateway: KrakenGateway) -> KrakenConnectorBridge:
    b = KrakenConnectorBridge(api_key="k", secret_key="c2VjcmV0")
    b._gateway = gateway
    return b


@pytest.fixture(params=["gateway", "bridge"])
def venue(request: pytest.FixtureRequest) -> Any:
    return request.getfixturevalue(request.param)


def _trail(kind: TriggerKind, distance: str) -> TriggerSpec:
    return TriggerSpec(
        kind=TriggerKind.ABSOLUTE,
        value=_D("1"),  # placeholder, ignored for trailing types
        trail=TrailSpec(kind=kind, distance=_D(distance)),
    )


_TYPES = [
    pytest.param(OrderType.TRAILING_STOP, None, id="trailing_stop"),
    pytest.param(OrderType.TRAILING_STOP_LIMIT, "-0.0005", id="trailing_stop_limit"),
]
_SIDES = [pytest.param(TradeType.BUY, id="buy"), pytest.param(TradeType.SELL, id="sell")]


def _limit(offset: str | None) -> Decimal | None:
    return None if offset is None else _D(offset)


class TestTriggerSpecPercentTrail:
    @pytest.mark.parametrize(("order_type", "offset"), _TYPES)
    @pytest.mark.parametrize("side", _SIDES)
    @pytest.mark.parametrize("fraction", ["0.0009", "0.0005", "0.00001"])
    async def test_below_minimum_rejected_before_http(
        self,
        venue: Any,
        rest: _RecordingRest,
        order_type: Any,
        offset: str | None,
        side: TradeType,
        fraction: str,
    ) -> None:
        kwargs: dict[str, Any] = {"trigger": _trail(TriggerKind.PERCENT, fraction)}
        if offset is not None:
            kwargs["limit_price"] = _limit(offset)
        with pytest.raises(UnsupportedOrderError) as excinfo:
            await venue.place_order(_PAIR, order_type, side, _AMOUNT, None, **kwargs)
        assert excinfo.value.exchange == "kraken"
        assert excinfo.value.feature == "percent_trail_min"
        assert rest.calls == []

    @pytest.mark.parametrize(
        ("fraction", "price"),
        [("0.001", "+0.1%"), ("0.0015", "+0.15%"), ("0.01", "+1%")],
    )
    async def test_at_or_above_minimum_accepted_trailing_stop(
        self, venue: Any, rest: _RecordingRest, fraction: str, price: str
    ) -> None:
        await venue.place_order(
            _PAIR,
            OrderType.TRAILING_STOP,
            TradeType.BUY,
            _AMOUNT,
            None,
            trigger=_trail(TriggerKind.PERCENT, fraction),
        )
        (body,) = rest.bodies
        assert body["price"] == price
        assert "price2" not in body

    async def test_boundary_accepted_with_small_limit_offset(
        self, venue: Any, rest: _RecordingRest
    ) -> None:
        await venue.place_order(
            _PAIR,
            OrderType.TRAILING_STOP_LIMIT,
            TradeType.SELL,
            _AMOUNT,
            None,
            limit_price=_D("-0.0005"),
            trigger=_trail(TriggerKind.PERCENT, "0.001"),
        )
        (body,) = rest.bodies
        assert body["price"] == "+0.1%"
        assert body["price2"] == "-0.05%"

    @pytest.mark.parametrize(("order_type", "offset"), _TYPES)
    async def test_context_rounded_fraction_below_minimum_rejected(
        self, venue: Any, rest: _RecordingRest, order_type: Any, offset: str | None
    ) -> None:
        # 31 significant digits: x100 rounds to exactly 0.1 at the default 28-digit context
        fraction = "0.000" + "9" * 30
        kwargs: dict[str, Any] = {"trigger": _trail(TriggerKind.PERCENT, fraction)}
        if offset is not None:
            kwargs["limit_price"] = _limit(offset)
        with pytest.raises(UnsupportedOrderError) as excinfo:
            await venue.place_order(_PAIR, order_type, TradeType.BUY, _AMOUNT, None, **kwargs)
        assert excinfo.value.feature == "percent_trail_min"
        assert rest.calls == []

    async def test_absolute_trail_unaffected(self, venue: Any, rest: _RecordingRest) -> None:
        await venue.place_order(
            _PAIR,
            OrderType.TRAILING_STOP,
            TradeType.BUY,
            _AMOUNT,
            None,
            trigger=_trail(TriggerKind.RELATIVE_OFFSET, "0.05"),
        )
        (body,) = rest.bodies
        assert body["price"] == "+0.05"


class TestLegacyPercentTrail:
    @pytest.mark.parametrize(("order_type", "offset"), _TYPES)
    @pytest.mark.parametrize("points", ["0.09", "0.05", "0.0001"])
    async def test_below_minimum_rejected_before_http(
        self,
        venue: Any,
        rest: _RecordingRest,
        order_type: Any,
        offset: str | None,
        points: str,
    ) -> None:
        kwargs: dict[str, Any] = {"price_in_percent": True}
        if offset is not None:
            kwargs["limit_price"] = _D("-0.05")
        with pytest.raises(UnsupportedOrderError) as excinfo:
            await venue.place_order(_PAIR, order_type, TradeType.BUY, _AMOUNT, _D(points), **kwargs)
        assert excinfo.value.exchange == "kraken"
        assert excinfo.value.feature == "percent_trail_min"
        assert rest.calls == []

    @pytest.mark.parametrize(("points", "price"), [("0.1", "+0.1%"), ("0.15", "+0.15%")])
    async def test_at_or_above_minimum_accepted(
        self, venue: Any, rest: _RecordingRest, points: str, price: str
    ) -> None:
        await venue.place_order(
            _PAIR,
            OrderType.TRAILING_STOP_LIMIT,
            TradeType.BUY,
            _AMOUNT,
            _D(points),
            limit_price=_D("-0.05"),
            price_in_percent=True,
        )
        (body,) = rest.bodies
        assert body["price"] == price
        assert body["price2"] == "-0.05%"

    @pytest.mark.parametrize("points", ["0.09", "0.0001", "0.0000001"])
    async def test_core_buy_sell_raise_synchronously(
        self, rest: _RecordingRest, gateway: KrakenGateway, points: str
    ) -> None:
        from market_connector.exchanges.kraken.hb_compat.kraken_connector_core import (
            KrakenConnectorCore,
        )
        from market_connector.hb_compat.event_bus import EventBus

        core = KrakenConnectorCore(gateway, event_bus=EventBus(name="trail-299"))
        for call in (core.buy, core.sell):
            with pytest.raises(UnsupportedOrderError) as excinfo:
                call(
                    _PAIR,
                    _AMOUNT,
                    OrderType.TRAILING_STOP,
                    _D(points),
                    price_in_percent=True,
                )
            assert excinfo.value.feature == "percent_trail_min"
        assert core.in_flight_orders == {}
        assert rest.calls == []

    async def test_absolute_small_distance_unaffected(
        self, venue: Any, rest: _RecordingRest
    ) -> None:
        await venue.place_order(_PAIR, OrderType.TRAILING_STOP, TradeType.BUY, _AMOUNT, _D("0.05"))
        (body,) = rest.bodies
        assert body["price"] == "+0.05"
