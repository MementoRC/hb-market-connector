# ruff: noqa: F811  (fixtures are imported from the #211 module and used as test parameters)
"""Kraken core limit_price / kwargs allowlist (issue #259), RED acceptance tests.

Pinned API::

    buy/sell(trading_pair, amount, order_type=LIMIT, price=NaN, *,
             limit_price=None, price_in_percent=False, position_action=None,
             expiration_ts=None) -> client_order_id

* ``*_LIMIT`` types send the limit as ``price2``; a missing ``limit_price`` raises
  ``ValueError`` synchronously, before anything is tracked or sent.
* Unknown kwargs and a non-``PositionAction`` ``position_action`` raise ``TypeError``.
* ``position_action`` and ``expiration_ts`` are accepted and not forwarded.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any
from unittest.mock import ANY

import pytest
from data_type_primitives.common import OrderType, PositionAction

from tests.exchanges.kraken.unit.test_kraken_connector_core_211 import (  # noqa: F401
    _AMOUNT,
    _PAIR,
    _PRICE,
    _WIRE_PAIR,
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

_LIMIT_TYPES = [
    OrderType.STOP_LOSS_LIMIT,
    OrderType.TAKE_PROFIT_LIMIT,
    OrderType.TRAILING_STOP_LIMIT,
]
_TRIGGER = Decimal("27000")
_LIMIT = Decimal("26950")
_WIRE_TYPE = {
    OrderType.STOP_LOSS_LIMIT: "stop-loss-limit",
    OrderType.TAKE_PROFIT_LIMIT: "take-profit-limit",
}


def _method(connector: Any, side: str) -> Any:
    return connector.buy if side == "buy" else connector.sell


def _created(side: str) -> int:
    return BUY_CREATED if side == "buy" else SELL_CREATED


async def _settle() -> None:
    """Let any scheduled background AddOrder task run."""
    await asyncio.sleep(0.05)


async def _assert_rejected_cleanly(
    connector: Any, rest: FakeKrakenRest, before: set[str], exc: type[Exception], call: Any
) -> None:
    with pytest.raises(exc):
        call()
    await _settle()
    assert rest.bodies("add_order") == []
    assert set(connector.in_flight_orders) == before


class TestLimitPriceWire:
    @pytest.mark.parametrize("side", ["buy", "sell"])
    @pytest.mark.parametrize("order_type", [OrderType.STOP_LOSS_LIMIT, OrderType.TAKE_PROFIT_LIMIT])
    async def test_conditional_limit_sends_price_and_price2(
        self,
        connector: Any,
        rest: FakeKrakenRest,
        seen: Recorder,
        side: str,
        order_type: OrderType,
    ) -> None:
        _method(connector, side)(_PAIR, _AMOUNT, order_type, _TRIGGER, limit_price=_LIMIT)
        await seen.wait(_created(side))
        (body,) = rest.bodies("add_order")
        assert body["ordertype"] == _WIRE_TYPE[order_type]
        assert body["type"] == side
        assert body["price"] == str(_TRIGGER)
        assert body["price2"] == str(_LIMIT)

    @pytest.mark.parametrize("side", ["buy", "sell"])
    @pytest.mark.parametrize(
        ("limit_price", "expected"),
        [(Decimal("-0.5"), "-0.5"), (Decimal("0.5"), "+0.5")],
    )
    async def test_trailing_stop_limit_sends_signed_offset_price2(
        self,
        connector: Any,
        rest: FakeKrakenRest,
        seen: Recorder,
        side: str,
        limit_price: Decimal,
        expected: str,
    ) -> None:
        _method(connector, side)(
            _PAIR,
            _AMOUNT,
            OrderType.TRAILING_STOP_LIMIT,
            Decimal("2.5"),
            limit_price=limit_price,
        )
        await seen.wait(_created(side))
        (body,) = rest.bodies("add_order")
        assert body["ordertype"] == "trailing-stop-limit"
        assert body["price"] == "+2.5"
        assert body["price2"] == expected

    async def test_trailing_stop_limit_percent_offsets(
        self, connector: Any, rest: FakeKrakenRest, seen: Recorder
    ) -> None:
        connector.sell(
            _PAIR,
            _AMOUNT,
            OrderType.TRAILING_STOP_LIMIT,
            Decimal("2.5"),
            limit_price=Decimal("-0.5"),
            price_in_percent=True,
        )
        await seen.wait(SELL_CREATED)
        (body,) = rest.bodies("add_order")
        assert body["price"] == "+2.5%"
        assert body["price2"] == "-0.5%"


class TestMissingLimitPrice:
    @pytest.mark.parametrize("side", ["buy", "sell"])
    @pytest.mark.parametrize("order_type", _LIMIT_TYPES)
    async def test_limit_type_without_limit_price_raises_value_error(
        self, connector: Any, rest: FakeKrakenRest, side: str, order_type: OrderType
    ) -> None:
        before = set(connector.in_flight_orders)
        await _assert_rejected_cleanly(
            connector,
            rest,
            before,
            ValueError,
            lambda: _method(connector, side)(_PAIR, _AMOUNT, order_type, _TRIGGER),
        )

    @pytest.mark.parametrize("order_type", _LIMIT_TYPES)
    async def test_explicit_none_limit_price_raises_value_error(
        self, connector: Any, rest: FakeKrakenRest, order_type: OrderType
    ) -> None:
        before = set(connector.in_flight_orders)
        await _assert_rejected_cleanly(
            connector,
            rest,
            before,
            ValueError,
            lambda: connector.buy(_PAIR, _AMOUNT, order_type, _TRIGGER, limit_price=None),
        )


class TestPercent:
    async def test_price_in_percent_forwarded_for_trailing_stop(
        self, connector: Any, rest: FakeKrakenRest, seen: Recorder
    ) -> None:
        connector.sell(
            _PAIR, _AMOUNT, OrderType.TRAILING_STOP, Decimal("2.5"), price_in_percent=True
        )
        await seen.wait(SELL_CREATED)
        (body,) = rest.bodies("add_order")
        assert body["ordertype"] == "trailing-stop"
        assert body["price"] == "+2.5%"

    async def test_price_in_percent_forwarded_for_stop_loss(
        self, connector: Any, rest: FakeKrakenRest, seen: Recorder
    ) -> None:
        connector.sell(_PAIR, _AMOUNT, OrderType.STOP_LOSS, Decimal("5"), price_in_percent=True)
        await seen.wait(SELL_CREATED)
        (body,) = rest.bodies("add_order")
        assert body["ordertype"] == "stop-loss"
        assert body["price"] == "#5%"


class TestKwargAllowlist:
    @pytest.mark.parametrize("side", ["buy", "sell"])
    @pytest.mark.parametrize(
        "kwargs",
        [
            pytest.param({"foo": 1}, id="foo"),
            pytest.param({"limit_prise": Decimal("1")}, id="typo"),
        ],
    )
    async def test_unknown_kwarg_raises_type_error(
        self, connector: Any, rest: FakeKrakenRest, side: str, kwargs: dict[str, Any]
    ) -> None:
        before = set(connector.in_flight_orders)
        await _assert_rejected_cleanly(
            connector,
            rest,
            before,
            TypeError,
            lambda: _method(connector, side)(_PAIR, _AMOUNT, OrderType.LIMIT, _PRICE, **kwargs),
        )

    @pytest.mark.parametrize("position_action", [*PositionAction, None])
    @pytest.mark.parametrize("side", ["buy", "sell"])
    async def test_position_action_accepted_and_ignored(
        self,
        connector: Any,
        rest: FakeKrakenRest,
        seen: Recorder,
        side: str,
        position_action: PositionAction | None,
    ) -> None:
        _method(connector, side)(
            _PAIR, _AMOUNT, OrderType.LIMIT, _PRICE, position_action=position_action
        )
        await seen.wait(_created(side))
        (body,) = rest.bodies("add_order")
        assert "position_action" not in body
        assert set(body) == {"pair", "type", "ordertype", "volume", "price", "userref"}

    @pytest.mark.parametrize("bad", ["CLOSE", 1])
    @pytest.mark.parametrize("side", ["buy", "sell"])
    async def test_non_position_action_raises_type_error(
        self, connector: Any, rest: FakeKrakenRest, side: str, bad: Any
    ) -> None:
        before = set(connector.in_flight_orders)
        await _assert_rejected_cleanly(
            connector,
            rest,
            before,
            TypeError,
            lambda: _method(connector, side)(
                _PAIR, _AMOUNT, OrderType.LIMIT, _PRICE, position_action=bad
            ),
        )

    async def test_expiration_ts_float_accepted_and_ignored(
        self, connector: Any, rest: FakeKrakenRest, seen: Recorder
    ) -> None:
        connector.buy(_PAIR, _AMOUNT, OrderType.LIMIT, _PRICE, expiration_ts=1_700_000_000.0)
        await seen.wait(BUY_CREATED)
        (body,) = rest.bodies("add_order")
        assert set(body) == {"pair", "type", "ordertype", "volume", "price", "userref"}


class TestRegression:
    async def test_plain_limit_buy_body_unchanged(
        self, connector: Any, rest: FakeKrakenRest, seen: Recorder
    ) -> None:
        connector.buy(_PAIR, _AMOUNT, OrderType.LIMIT, _PRICE)
        await seen.wait(BUY_CREATED)
        assert rest.bodies("add_order") == [
            {
                "pair": _WIRE_PAIR,
                "type": "buy",
                "ordertype": "limit",
                "volume": "0.5",
                "price": "27000",
                "userref": ANY,
            }
        ]


class TestPerOrderLimitPrice:
    async def test_concurrent_orders_keep_their_own_price2(
        self, connector: Any, rest: FakeKrakenRest, seen: Recorder
    ) -> None:
        connector.buy(
            _PAIR, _AMOUNT, OrderType.STOP_LOSS_LIMIT, _TRIGGER, limit_price=Decimal("26950")
        )
        connector.buy(
            _PAIR, _AMOUNT, OrderType.STOP_LOSS_LIMIT, _TRIGGER, limit_price=Decimal("26900")
        )
        await seen.wait(BUY_CREATED, count=2)
        assert sorted(b["price2"] for b in rest.bodies("add_order")) == ["26900", "26950"]


class TestParentAdapterCallShape:
    @pytest.mark.parametrize("side", ["buy", "sell"])
    async def test_parent_adapter_call_shape_all_kwargs(
        self, connector: Any, rest: FakeKrakenRest, seen: Recorder, side: str
    ) -> None:
        """Parent adapter (market_connector_exchange.py _create_order, CORE_ORDER_KWARGS) call shape; all four kwargs must be accepted and none may reach HBOrder (extra=forbid, #290)."""  # noqa: E501
        _method(connector, side)(
            _PAIR,
            _AMOUNT,
            OrderType.STOP_LOSS_LIMIT,
            Decimal("95"),
            limit_price=Decimal("94"),
            price_in_percent=False,
            position_action=PositionAction.OPEN,
            expiration_ts=1_700_000_000.0,
        )
        await seen.wait(_created(side))
        (body,) = rest.bodies("add_order")
        assert body["ordertype"] == "stop-loss-limit"
        assert body["type"] == side
        assert body["price"] == "95"
        assert body["price2"] == "94"
        assert "%" not in body["price2"]
