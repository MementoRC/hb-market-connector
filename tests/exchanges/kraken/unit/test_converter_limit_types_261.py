# ruff: noqa: F811  (fixtures are imported from the #211 module and used as test parameters)
"""Kraken converters: TAKE_PROFIT_LIMIT / TRAILING_STOP_LIMIT (issue #261), RED acceptance tests.

``converters._HB_TO_KRAKEN`` must agree with the gateway ``mixins.orders._ORDER_TYPE_MAP``
so that ``kraken_ordertype_from_hb`` accepts every type the gateway advertises.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from data_type_primitives.common import OrderType

from market_connector.exchanges.kraken.converters import (
    _HB_TO_KRAKEN,
    kraken_ordertype_from_hb,
)
from market_connector.exchanges.kraken.mixins.orders import _ORDER_TYPE_MAP
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

_NEW_TYPES = [
    (OrderType.TAKE_PROFIT_LIMIT, "take-profit-limit"),
    (OrderType.TRAILING_STOP_LIMIT, "trailing-stop-limit"),
]

# Hard-coded snapshot of the entries present before #261.
_EXISTING_SNAPSHOT = {
    "LIMIT": "limit",
    "MARKET": "market",
    "STOP_LOSS": "stop-loss",
    "STOP_LOSS_LIMIT": "stop-loss-limit",
    "TAKE_PROFIT": "take-profit",
    "TRAILING_STOP": "trailing-stop",
    "LIMIT_MAKER": "limit",
}


class TestConverterNewTypes:
    @pytest.mark.parametrize(("order_type", "expected"), _NEW_TYPES)
    def test_enum_maps_to_kraken_string(self, order_type: OrderType, expected: str) -> None:
        assert kraken_ordertype_from_hb(order_type) == expected

    @pytest.mark.parametrize(("order_type", "expected"), _NEW_TYPES)
    @pytest.mark.parametrize("variant", ["upper", "lower", "hyphen"])
    def test_string_forms_map_to_kraken_string(
        self, order_type: OrderType, expected: str, variant: str
    ) -> None:
        name = order_type.name
        text = {"upper": name, "lower": name.lower(), "hyphen": name.lower().replace("_", "-")}[
            variant
        ]
        assert kraken_ordertype_from_hb(text) == expected


class TestSingleSourceOfTruth:
    def test_converter_map_agrees_with_gateway_map(self) -> None:
        assert _HB_TO_KRAKEN == _ORDER_TYPE_MAP

    @pytest.mark.parametrize("key", sorted(_ORDER_TYPE_MAP))
    def test_every_gateway_type_converts_to_gateway_wire_value(self, key: str) -> None:
        assert kraken_ordertype_from_hb(key) == _ORDER_TYPE_MAP[key]


class TestRegressionSnapshot:
    @pytest.mark.parametrize(("key", "expected"), sorted(_EXISTING_SNAPSHOT.items()))
    def test_existing_entry_unchanged(self, key: str, expected: str) -> None:
        assert _HB_TO_KRAKEN[key] == expected
        assert kraken_ordertype_from_hb(key) == expected

    def test_unknown_type_still_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Cannot map order type"):
            kraken_ordertype_from_hb("NOT_A_TYPE")


class TestCoreEndToEnd:
    @pytest.mark.parametrize("side", ["buy", "sell"])
    @pytest.mark.parametrize(("order_type", "expected"), _NEW_TYPES)
    async def test_core_reaches_add_order_with_wire_type(
        self,
        connector: Any,
        rest: FakeKrakenRest,
        seen: Recorder,
        side: str,
        order_type: OrderType,
        expected: str,
    ) -> None:
        method = connector.buy if side == "buy" else connector.sell
        method(_PAIR, _AMOUNT, order_type, Decimal("27000"), limit_price=Decimal("26950"))
        await seen.wait(BUY_CREATED if side == "buy" else SELL_CREATED)
        (body,) = rest.bodies("add_order")
        assert body["ordertype"] == expected
        assert body["type"] == side
        # Trailing types send a signed offset for price2 (see #259).
        assert body["price2"] == ("+26950" if "TRAILING" in order_type.name else "26950")
