"""RED acceptance tests for issue #240 (criterion 1): Kraken adopts ``TriggerSpec``.

Contract pinned here (none of it exists yet):

* ``KrakenGateway.place_order(..., trigger: TriggerSpec | None = None)`` (keyword-only).
  With a ``trigger`` the AddOrder body must be byte-identical to today's
  ``price`` / ``price_in_percent`` / ``limit_price`` path:

  - STOP_LOSS / TAKE_PROFIT: ``price`` is derived from the trigger (ABSOLUTE -> ``"49000"``,
    PERCENT fraction 0.02 -> ``"#2%"``).
  - ``*_LIMIT``: ``price`` argument is the limit price (HBOrder convention) and is sent as
    ``price2``; the trigger is sent as ``price``.
  - TRAILING_*: the distance comes from ``trigger.trail`` (RELATIVE_OFFSET -> ``"+150.5"``,
    PERCENT fraction 0.015 -> ``"+1.5%"``); ``TriggerSpec.value`` is ignored. For
    TRAILING_STOP_LIMIT the signed offset stays in ``limit_price``.
* ``trigger`` together with ``price_in_percent`` is a ``ValueError``; so is a trigger on a
  non-conditional type, a trailing type without ``trail`` or any other type with one.

Golden payloads: ``_LEGACY`` calls were run at base 8a217b3 and their bodies pinned in
``_EXPECTED``; the legacy-anchor tests pass today, the trigger tests are RED.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from data_type_primitives import TrailSpec, TriggerKind, TriggerSpec
from data_type_primitives.common import OrderType

from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway
from market_connector.primitives import TradeType
from market_connector.transport.response import Response

_ADD_ORDER_OK = {"error": [], "result": {"descr": {"order": "x"}, "txid": ["OABC-XXXXX-YYYYYY"]}}
_PAIR = "XBTUSD"
_AMOUNT = Decimal("0.5")
_D = Decimal


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


def _abs(value: str) -> TriggerSpec:
    return TriggerSpec(kind=TriggerKind.ABSOLUTE, value=_D(value))


def _pct(fraction: str) -> TriggerSpec:
    return TriggerSpec(kind=TriggerKind.PERCENT, value=_D(fraction))


def _trail(kind: TriggerKind, distance: str) -> TriggerSpec:
    # ``value`` is irrelevant for trailing types: the distance lives in ``trail``.
    return TriggerSpec(
        kind=TriggerKind.ABSOLUTE, value=_D("1"), trail=TrailSpec(kind=kind, distance=_D(distance))
    )


# id -> (order type, legacy kwargs, trigger kwargs, expected extra body fields)
_CASES: dict[str, tuple[OrderType, dict[str, Any], dict[str, Any], dict[str, str]]] = {
    "stop-loss-abs": (
        OrderType.STOP_LOSS,
        {"price": _D("49000")},
        {"trigger": _abs("49000")},
        {"ordertype": "stop-loss", "price": "49000"},
    ),
    "take-profit-abs": (
        OrderType.TAKE_PROFIT,
        {"price": _D("51000")},
        {"trigger": _abs("51000")},
        {"ordertype": "take-profit", "price": "51000"},
    ),
    "stop-loss-percent": (
        OrderType.STOP_LOSS,
        {"price": _D("2"), "price_in_percent": True},
        {"trigger": _pct("0.02")},
        {"ordertype": "stop-loss", "price": "#2%"},
    ),
    "take-profit-percent": (
        OrderType.TAKE_PROFIT,
        {"price": _D("1.5"), "price_in_percent": True},
        {"trigger": _pct("0.015")},
        {"ordertype": "take-profit", "price": "#1.5%"},
    ),
    "stop-loss-limit-abs": (
        OrderType.STOP_LOSS_LIMIT,
        {"price": _D("49000"), "limit_price": _D("48900")},
        {"price": _D("48900"), "trigger": _abs("49000")},
        {"ordertype": "stop-loss-limit", "price": "49000", "price2": "48900"},
    ),
    "take-profit-limit-abs": (
        OrderType.TAKE_PROFIT_LIMIT,
        {"price": _D("51000"), "limit_price": _D("51100")},
        {"price": _D("51100"), "trigger": _abs("51000")},
        {"ordertype": "take-profit-limit", "price": "51000", "price2": "51100"},
    ),
    "stop-loss-limit-percent": (
        OrderType.STOP_LOSS_LIMIT,
        {"price": _D("2"), "limit_price": _D("48900"), "price_in_percent": True},
        {"price": _D("48900"), "trigger": _pct("0.02")},
        {"ordertype": "stop-loss-limit", "price": "#2%", "price2": "48900"},
    ),
    "trailing-stop-abs": (
        OrderType.TRAILING_STOP,
        {"price": _D("150.5")},
        {"trigger": _trail(TriggerKind.RELATIVE_OFFSET, "150.5")},
        {"ordertype": "trailing-stop", "price": "+150.5"},
    ),
    "trailing-stop-percent": (
        OrderType.TRAILING_STOP,
        {"price": _D("1.5"), "price_in_percent": True},
        {"trigger": _trail(TriggerKind.PERCENT, "0.015")},
        {"ordertype": "trailing-stop", "price": "+1.5%"},
    ),
    "trailing-stop-limit-abs": (
        OrderType.TRAILING_STOP_LIMIT,
        {"price": _D("150.5"), "limit_price": _D("-5")},
        {"limit_price": _D("-5"), "trigger": _trail(TriggerKind.RELATIVE_OFFSET, "150.5")},
        {"ordertype": "trailing-stop-limit", "price": "+150.5", "price2": "-5"},
    ),
}
_PARAMS = [pytest.param(k, id=k) for k in _CASES]


def _expected(case: str) -> dict[str, str]:
    return {"pair": _PAIR, "type": "buy", "volume": "0.5", **_CASES[case][3]}


async def _place(gateway: KrakenGateway, case: str, *, legacy: bool) -> None:
    order_type, legacy_kwargs, trigger_kwargs, _ = _CASES[case]
    kwargs = dict(legacy_kwargs if legacy else trigger_kwargs)
    price = kwargs.pop("price", None)
    await gateway.place_order(_PAIR, order_type, TradeType.BUY, _AMOUNT, price, **kwargs)


@pytest.mark.parametrize("case", _PARAMS)
class TestGoldenPayloads:
    async def test_legacy_path_payload_is_pinned(
        self, gateway: KrakenGateway, rest: _RecordingRest, case: str
    ) -> None:
        """Parity anchor: passes at base; pins today's wire format."""
        await _place(gateway, case, legacy=True)
        assert rest.bodies == [_expected(case)]

    async def test_trigger_path_matches_golden_payload(
        self, gateway: KrakenGateway, rest: _RecordingRest, case: str
    ) -> None:
        await _place(gateway, case, legacy=False)
        assert rest.bodies == [_expected(case)]

    async def test_trigger_path_equals_legacy_path(
        self, gateway: KrakenGateway, rest: _RecordingRest, case: str
    ) -> None:
        await _place(gateway, case, legacy=True)
        await _place(gateway, case, legacy=False)
        legacy_body, trigger_body = rest.bodies
        assert trigger_body == legacy_body


class TestTriggerValidation:
    async def test_trigger_with_price_in_percent_is_rejected(
        self, gateway: KrakenGateway, rest: _RecordingRest
    ) -> None:
        with pytest.raises(ValueError, match=r"(?i)percent|trigger"):
            await gateway.place_order(
                _PAIR,
                OrderType.STOP_LOSS,
                TradeType.BUY,
                _AMOUNT,
                None,
                trigger=_pct("0.02"),
                price_in_percent=True,
            )
        assert rest.bodies == []

    @pytest.mark.parametrize(
        ("order_type", "price"), [(OrderType.MARKET, None), (OrderType.LIMIT, _D("50000"))]
    )
    async def test_trigger_on_non_conditional_type_is_rejected(
        self, gateway: KrakenGateway, rest: _RecordingRest, order_type: OrderType, price: Any
    ) -> None:
        with pytest.raises(ValueError, match=r"(?i)trigger"):
            await gateway.place_order(
                _PAIR, order_type, TradeType.BUY, _AMOUNT, price, trigger=_abs("49000")
            )
        assert rest.bodies == []

    @pytest.mark.parametrize("order_type", [OrderType.TRAILING_STOP, OrderType.TRAILING_STOP_LIMIT])
    async def test_trailing_without_trail_is_rejected(
        self, gateway: KrakenGateway, rest: _RecordingRest, order_type: OrderType
    ) -> None:
        extra = {"limit_price": _D("-5")} if order_type is OrderType.TRAILING_STOP_LIMIT else {}
        with pytest.raises(ValueError, match=r"(?i)trail"):
            await gateway.place_order(
                _PAIR, order_type, TradeType.BUY, _AMOUNT, None, trigger=_abs("49000"), **extra
            )
        assert rest.bodies == []

    async def test_trail_on_non_trailing_type_is_rejected(
        self, gateway: KrakenGateway, rest: _RecordingRest
    ) -> None:
        with pytest.raises(ValueError, match=r"(?i)trail"):
            await gateway.place_order(
                _PAIR,
                OrderType.STOP_LOSS,
                TradeType.BUY,
                _AMOUNT,
                None,
                trigger=_trail(TriggerKind.RELATIVE_OFFSET, "150.5"),
            )
        assert rest.bodies == []
