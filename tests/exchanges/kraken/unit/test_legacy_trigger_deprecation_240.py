# ruff: noqa: F811  (fixtures are imported from the #211 module and used as test parameters)
"""Legacy trigger-arg deprecation at the Kraken gateway (#240, Sourcery thread on PR #279).

A TRIGGERED order type placed through ``KrakenGateway.place_order`` without ``trigger=``
relies on the overloaded ``price`` / ``limit_price`` / ``price_in_percent`` arguments: that
emits one ``DeprecationWarning`` per call (removal in #241). The request body is unchanged.
The hb_compat bridge and core never emit it.
"""

from __future__ import annotations

import asyncio
import warnings
from decimal import Decimal
from typing import Any

import pytest
from data_type_primitives import TrailSpec, TriggerKind, TriggerSpec
from data_type_primitives.common import OrderType

from market_connector.exchanges.kraken.hb_compat.kraken_bridge import KrakenConnectorBridge
from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway
from market_connector.primitives import TradeType
from tests.exchanges.kraken.unit.test_kraken_connector_core_211 import (  # noqa: F401
    _PAIR,
    FakeKrakenRest,
    Recorder,
    bare_connector,
    bus,
    connector,
    gateway,
    rest,
    seen,
)

_MATCH = r"(?s)TriggerSpec.*#241"
_AMOUNT = Decimal("0.5")
_D = Decimal
_XBT = "XBTUSD"

# (order_type, price, limit_price, price_in_percent) legacy-arg calls.
_LEGACY: dict[str, tuple[OrderType, Decimal, Decimal | None, bool]] = {
    "stop_loss": (OrderType.STOP_LOSS, _D("49000"), None, False),
    "take_profit": (OrderType.TAKE_PROFIT, _D("51000"), None, False),
    "stop_loss_limit": (OrderType.STOP_LOSS_LIMIT, _D("49000"), _D("48950"), False),
    "take_profit_limit": (OrderType.TAKE_PROFIT_LIMIT, _D("51000"), _D("51050"), False),
    "trailing_stop": (OrderType.TRAILING_STOP, _D("150.5"), None, False),
    "trailing_stop_limit": (OrderType.TRAILING_STOP_LIMIT, _D("150.5"), _D("-10"), False),
    "stop_loss_percent": (OrderType.STOP_LOSS, _D("2"), None, True),
    "trailing_stop_percent": (OrderType.TRAILING_STOP, _D("1.5"), None, True),
}


async def _place(gateway: KrakenGateway, case: str, **extra: Any) -> str:
    order_type, price, limit_price, percent = _LEGACY[case]
    return await gateway.place_order(
        _XBT,
        order_type,
        TradeType.SELL,
        _AMOUNT,
        price,
        limit_price=limit_price,
        price_in_percent=percent,
        **extra,
    )


def _deprecations(caught: list[warnings.WarningMessage]) -> list[warnings.WarningMessage]:
    return [w for w in caught if issubclass(w.category, DeprecationWarning)]


class TestLegacyTriggerWarns:
    @pytest.mark.parametrize("case", sorted(_LEGACY))
    async def test_warns_once_per_call(self, gateway: KrakenGateway, case: str) -> None:
        with pytest.warns(DeprecationWarning, match=_MATCH) as record:
            await _place(gateway, case)
        assert len(_deprecations(list(record))) == 1

    async def test_warning_points_at_the_caller(self, gateway: KrakenGateway) -> None:
        with pytest.warns(DeprecationWarning) as record:
            await _place(gateway, "stop_loss")
        assert record[0].filename == __file__

    async def test_each_call_warns(self, gateway: KrakenGateway) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            await _place(gateway, "stop_loss")
            await _place(gateway, "stop_loss")
        assert len(_deprecations(caught)) == 2

    async def test_string_order_type_warns(self, gateway: KrakenGateway) -> None:
        with pytest.warns(DeprecationWarning, match=_MATCH):
            await gateway.place_order(_XBT, "STOP_LOSS", "sell", _AMOUNT, _D("49000"))


class TestNoWarning:
    async def test_trigger_kwarg_is_silent(self, gateway: KrakenGateway) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            await gateway.place_order(
                _XBT,
                OrderType.STOP_LOSS,
                TradeType.SELL,
                _AMOUNT,
                trigger=TriggerSpec(kind=TriggerKind.ABSOLUTE, value=_D("49000")),
            )
            await gateway.place_order(
                _XBT,
                OrderType.TRAILING_STOP,
                TradeType.SELL,
                _AMOUNT,
                trigger=TriggerSpec(
                    kind=TriggerKind.ABSOLUTE,
                    value=_D("1"),  # ignored for trailing types
                    trail=TrailSpec(kind=TriggerKind.RELATIVE_OFFSET, distance=_D("150.5")),
                ),
            )

    @pytest.mark.parametrize(
        "order_type", [OrderType.LIMIT, OrderType.LIMIT_MAKER, OrderType.MARKET]
    )
    async def test_non_triggered_types_are_silent(
        self, gateway: KrakenGateway, order_type: OrderType
    ) -> None:
        price = None if order_type is OrderType.MARKET else _D("50000")
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            await gateway.place_order(_XBT, order_type, TradeType.BUY, _AMOUNT, price)


class TestHbCompatIsSilent:
    @pytest.mark.parametrize("case", sorted(_LEGACY))
    async def test_bridge_is_silent_and_sends_same_body(
        self, gateway: KrakenGateway, rest: FakeKrakenRest, case: str
    ) -> None:
        bridge = KrakenConnectorBridge(api_key="k", secret_key="c2VjcmV0")
        bridge._gateway = gateway
        order_type, price, limit_price, percent = _LEGACY[case]
        with pytest.warns(DeprecationWarning, match=_MATCH):
            await _place(gateway, case)
        direct = rest.bodies("add_order")[-1]
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            await bridge.place_order(
                _XBT,
                order_type,
                TradeType.SELL,
                _AMOUNT,
                price,
                limit_price=limit_price,
                price_in_percent=percent,
            )
        assert rest.bodies("add_order")[-1] == direct

    @pytest.mark.parametrize(
        ("order_type", "price", "limit_price", "percent"),
        [
            (OrderType.STOP_LOSS, _D("27000"), None, False),
            (OrderType.TAKE_PROFIT_LIMIT, _D("27000"), _D("27050"), False),
            (OrderType.TRAILING_STOP, _D("150.5"), None, False),
            (OrderType.STOP_LOSS, _D("2"), None, True),
        ],
    )
    async def test_core_buy_is_silent(
        self,
        connector: Any,
        rest: FakeKrakenRest,
        order_type: OrderType,
        price: Decimal,
        limit_price: Decimal | None,
        percent: bool,
    ) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            connector.buy(
                _PAIR,
                _AMOUNT,
                order_type,
                price,
                limit_price=limit_price,
                price_in_percent=percent,
            )
            await asyncio.sleep(0.05)
        assert rest.bodies("add_order")
        assert _deprecations(caught) == []


class TestPayloadUnchanged:
    @pytest.mark.parametrize("case", sorted(_LEGACY))
    async def test_body_identical_with_warning_ignored_or_recorded(
        self, gateway: KrakenGateway, rest: FakeKrakenRest, case: str
    ) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            await _place(gateway, case)
        with pytest.warns(DeprecationWarning):
            await _place(gateway, case)
        ignored, recorded = rest.bodies("add_order")
        assert ignored == recorded
