# ruff: noqa: F811  (fixtures are imported from the #211 module and used as test parameters)
"""PR #294 review: RED acceptance for trigger quantization, created-event price, protocol kwarg.

* B1: ``trigger=`` values are floored to the pair's price tick (``pair_decimals`` 1 for BTC-USD),
  exactly like the legacy form through ``quantize_order``; PERCENT kinds are not quantized.
* B2: the created event's price under ``trigger=`` equals the legacy form's (never NaN).
* B4: ``ConnectorCoreProtocol.buy/sell`` declare a keyword-only ``trigger=None``.
"""

from __future__ import annotations

import inspect
import math
import warnings
from decimal import Decimal
from typing import Any

import pytest
from data_type_primitives import TrailSpec, TriggerKind, TriggerSpec
from data_type_primitives.common import OrderType

from market_connector.hb_compat.core_protocol import ConnectorCoreProtocol
from tests.exchanges.kraken.unit.test_kraken_connector_core_211 import (  # noqa: F401
    _AMOUNT,
    _PAIR,
    BUY_CREATED,
    SELL_CREATED,
    FakeKrakenRest,
    Recorder,
    bare_connector,
    bus,
    connector,
    gateway,
    rest,
    seen,
)

_D = Decimal
_UNIQUE = {"userref", "cl_ord_id"}


def _abs(value: str) -> TriggerSpec:
    return TriggerSpec(kind=TriggerKind.ABSOLUTE, value=_D(value))


def _trail(distance: str, kind: TriggerKind = TriggerKind.RELATIVE_OFFSET) -> TriggerSpec:
    return TriggerSpec(
        kind=TriggerKind.ABSOLUTE,
        value=_D("1"),
        trail=TrailSpec(kind=kind, distance=_D(distance)),
    )


# case -> (order_type, legacy (price, limit_price), trigger (price, limit_price, spec))
_OFF_TICK: dict[str, tuple[Any, tuple[Any, Any], tuple[Any, Any, TriggerSpec]]] = {
    "stop_loss": (OrderType.STOP_LOSS, (_D("27000.123"), None), (None, None, _abs("27000.123"))),
    "take_profit": (
        OrderType.TAKE_PROFIT,
        (_D("27000.123"), None),
        (None, None, _abs("27000.123")),
    ),
    "stop_loss_limit": (
        OrderType.STOP_LOSS_LIMIT,
        (_D("27000.123"), _D("26990.07")),
        (_D("26990.07"), None, _abs("27000.123")),
    ),
    "take_profit_limit": (
        OrderType.TAKE_PROFIT_LIMIT,
        (_D("27000.123"), _D("27050.07")),
        (_D("27050.07"), None, _abs("27000.123")),
    ),
    "trailing_stop": (
        OrderType.TRAILING_STOP,
        (_D("15.07"), None),
        (None, None, _trail("15.07")),
    ),
    "trailing_stop_limit": (
        OrderType.TRAILING_STOP_LIMIT,
        (_D("15.07"), _D("-0.5")),
        (None, _D("-0.5"), _trail("15.07")),
    ),
}
_NO_PRICE = ["stop_loss", "take_profit", "trailing_stop"]
_EXPECTED_EVENT_PRICE = {
    "stop_loss": _D("27000.1"),
    "take_profit": _D("27000.1"),
    "trailing_stop": _D("15.0"),
}


def _comparable(body: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in body.items() if k not in _UNIQUE}


async def _buy(
    connector: Any,
    seen: Recorder,
    order_type: Any,
    price: Decimal | None,
    limit_price: Decimal | None,
    trigger: TriggerSpec | None,
    side: str = "buy",
) -> None:
    kwargs: dict[str, Any] = {}
    if limit_price is not None:
        kwargs["limit_price"] = limit_price
    if trigger is not None:
        kwargs["trigger"] = trigger
    args = (price,) if price is not None else ()
    created_id = BUY_CREATED if side == "buy" else SELL_CREATED
    count = len(seen.of(created_id)) + 1
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        getattr(connector, side)(_PAIR, _AMOUNT, order_type, *args, **kwargs)
    await seen.wait(created_id, count=count, timeout=2.0)


async def _legacy(connector: Any, seen: Recorder, case: str, side: str = "buy") -> None:
    order_type, (price, limit_price), _ = _OFF_TICK[case]
    await _buy(connector, seen, order_type, price, limit_price, None, side)


async def _trigger(connector: Any, seen: Recorder, case: str, side: str = "buy") -> None:
    order_type, _, (price, limit_price, spec) = _OFF_TICK[case]
    await _buy(connector, seen, order_type, price, limit_price, spec, side)


class TestTriggerQuantization:
    async def test_stop_loss_trigger_floored_to_tick(
        self, connector: Any, seen: Recorder, rest: FakeKrakenRest
    ) -> None:
        await _trigger(connector, seen, "stop_loss")
        assert rest.bodies("add_order")[0]["price"] == "27000.1"

    async def test_stop_loss_limit_trigger_and_limit_floored(
        self, connector: Any, seen: Recorder, rest: FakeKrakenRest
    ) -> None:
        await _trigger(connector, seen, "stop_loss_limit")
        body = rest.bodies("add_order")[0]
        assert body["price"] == "27000.1"
        assert body["price2"] == "26990.0"

    async def test_trailing_distance_floored_to_tick(
        self, connector: Any, seen: Recorder, rest: FakeKrakenRest
    ) -> None:
        await _trigger(connector, seen, "trailing_stop")
        price = rest.bodies("add_order")[0]["price"]
        assert price.lstrip("+") == "15.0"

    async def test_percent_trigger_not_quantized(
        self, connector: Any, seen: Recorder, rest: FakeKrakenRest
    ) -> None:
        spec = TriggerSpec(kind=TriggerKind.PERCENT, value=_D("0.0255"))  # fraction: 2.55%
        await _buy(connector, seen, OrderType.STOP_LOSS, None, None, spec)
        assert "2.55" in rest.bodies("add_order")[0]["price"]

    @pytest.mark.parametrize("case", sorted(_OFF_TICK))
    async def test_quantized_trigger_body_equals_legacy_body(
        self, connector: Any, seen: Recorder, rest: FakeKrakenRest, case: str
    ) -> None:
        await _legacy(connector, seen, case)
        await _trigger(connector, seen, case)
        legacy, via_trigger = rest.bodies("add_order")
        assert _comparable(via_trigger) == _comparable(legacy)


class TestCreatedEventPrice:
    @pytest.mark.parametrize("case", _NO_PRICE)
    async def test_event_price_is_quantized_trigger(
        self, connector: Any, seen: Recorder, rest: FakeKrakenRest, case: str
    ) -> None:
        await _trigger(connector, seen, case)
        (created,) = seen.of(BUY_CREATED)
        assert not math.isnan(created.price)
        assert created.price == _EXPECTED_EVENT_PRICE[case]
        body = rest.bodies("add_order")[0]
        assert body["ordertype"] == _OFF_TICK[case][0].name.lower().replace("_", "-")

    @pytest.mark.parametrize("case", sorted(_OFF_TICK))
    async def test_event_price_equals_legacy_event_price(
        self, connector: Any, seen: Recorder, case: str
    ) -> None:
        await _legacy(connector, seen, case)
        await _trigger(connector, seen, case)
        legacy, via_trigger = seen.of(BUY_CREATED)
        assert not math.isnan(via_trigger.price)
        assert via_trigger.price == legacy.price


class TestOffTickParity:
    @pytest.mark.parametrize("side", ["buy", "sell"])
    @pytest.mark.parametrize("case", sorted(_OFF_TICK))
    async def test_offtick_parity_legacy_vs_trigger_event_and_wire(
        self, connector: Any, seen: Recorder, rest: FakeKrakenRest, case: str, side: str
    ) -> None:
        created_id = BUY_CREATED if side == "buy" else SELL_CREATED
        await _legacy(connector, seen, case, side)
        await _trigger(connector, seen, case, side)
        legacy_event, trigger_event = seen.of(created_id)
        assert not math.isnan(trigger_event.price)
        assert trigger_event.price == legacy_event.price
        legacy_body, trigger_body = rest.bodies("add_order")
        assert trigger_body["price"] == legacy_body["price"]
        assert trigger_body.get("price2") == legacy_body.get("price2")


class TestProtocolTriggerKwarg:
    @pytest.mark.parametrize("method", ["buy", "sell"])
    def test_trigger_is_keyword_only_default_none(self, method: str) -> None:
        params = inspect.signature(getattr(ConnectorCoreProtocol, method)).parameters
        assert "trigger" in params
        assert params["trigger"].kind is inspect.Parameter.KEYWORD_ONLY
        assert params["trigger"].default is None
