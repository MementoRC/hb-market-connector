# ruff: noqa: F811  (fixtures are imported from the #211 module and used as test parameters)
"""``trigger=TriggerSpec`` on the Kraken hb_compat core and bridge (issue #288), RED acceptance.

Pinned behaviour (mirrors ``KrakenGateway.place_order`` from #240):

* ``KrakenConnectorCore.buy/sell`` and ``KrakenConnectorBridge.place_order`` accept
  ``trigger=TriggerSpec | None`` and forward it to the gateway.
* With ``trigger`` the argument convention is the gateway's: for STOP_LOSS_LIMIT /
  TAKE_PROFIT_LIMIT ``price`` is the limit price (sent as ``price2``) and the trigger is sent as
  ``price``; ``limit_price`` stays the signed offset of TRAILING_STOP_LIMIT; trailing types take
  the distance from ``trigger.trail`` and ``price`` is omitted.
* The trigger path is silent; the legacy triggered form through core/bridge now emits the
  gateway's ``DeprecationWarning`` (#241). This reverses
  ``test_legacy_trigger_deprecation_240.py::TestHbCompatIsSilent``.
* Validation matches the gateway, synchronously for the core (nothing tracked or sent).
"""

from __future__ import annotations

import asyncio
import warnings
from decimal import Decimal
from typing import Any

import pytest
from data_type_primitives import TrailSpec, TriggerKind, TriggerSpec
from data_type_primitives.common import OrderType

from market_connector.exceptions import UnsupportedOrderError
from market_connector.exchanges.kraken.hb_compat.kraken_bridge import KrakenConnectorBridge
from tests.exchanges.kraken.unit.test_kraken_connector_core_211 import (  # noqa: F401
    _AMOUNT,
    _PAIR,
    _WIRE_PAIR,
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
_MATCH = r"(?s)TriggerSpec.*#241"
_UNIQUE = {"userref", "cl_ord_id"}
_ENTRIES = ["core_buy", "core_sell", "bridge"]


def _abs(value: str) -> TriggerSpec:
    return TriggerSpec(kind=TriggerKind.ABSOLUTE, value=_D(value))


def _trail(kind: TriggerKind, distance: str) -> TriggerSpec:
    return TriggerSpec(
        kind=TriggerKind.ABSOLUTE,
        value=_D("1"),  # placeholder, ignored for trailing types
        trail=TrailSpec(kind=kind, distance=_D(distance)),
    )


# case -> (order_type, legacy (price, limit_price), trigger (price, limit_price, TriggerSpec))
_CASES: dict[str, tuple[Any, tuple[Any, Any], tuple[Any, Any, TriggerSpec]]] = {
    "stop_loss": (OrderType.STOP_LOSS, (_D("27000"), None), (None, None, _abs("27000"))),
    "take_profit": (OrderType.TAKE_PROFIT, (_D("27000"), None), (None, None, _abs("27000"))),
    "stop_loss_limit": (
        OrderType.STOP_LOSS_LIMIT,
        (_D("27000"), _D("26950")),
        (_D("26950"), None, _abs("27000")),
    ),
    "take_profit_limit": (
        OrderType.TAKE_PROFIT_LIMIT,
        (_D("27000"), _D("27050")),
        (_D("27050"), None, _abs("27000")),
    ),
    "trailing_stop": (
        OrderType.TRAILING_STOP,
        (_D("150"), None),
        (None, None, _trail(TriggerKind.RELATIVE_OFFSET, "150")),
    ),
    "trailing_stop_limit": (
        OrderType.TRAILING_STOP_LIMIT,
        (_D("150"), _D("-10")),
        (None, _D("-10"), _trail(TriggerKind.RELATIVE_OFFSET, "150")),
    ),
}


class _Entry:
    """Uniform ``place`` over core buy/sell and bridge; ``pair`` is the entry's own pair form."""

    def __init__(self, name: str, connector: Any, bridge: KrakenConnectorBridge) -> None:
        self.name = name
        self._connector = connector
        self._bridge = bridge

    @property
    def side(self) -> str:
        return "sell" if self.name == "core_sell" else "buy"

    async def place(
        self,
        order_type: Any,
        price: Decimal | None = None,
        *,
        limit_price: Decimal | None = None,
        price_in_percent: bool = False,
        trigger: TriggerSpec | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {}
        if limit_price is not None:
            kwargs["limit_price"] = limit_price
        if price_in_percent:
            kwargs["price_in_percent"] = True
        if trigger is not None:
            kwargs["trigger"] = trigger
        if self.name == "bridge":
            await self._bridge.place_order(_WIRE_PAIR, order_type, "buy", _AMOUNT, price, **kwargs)
            return
        method = self._connector.sell if self.name == "core_sell" else self._connector.buy
        args = (price,) if price is not None else ()
        method(_PAIR, _AMOUNT, order_type, *args, **kwargs)
        await asyncio.sleep(0.05)  # let the background AddOrder run


@pytest.fixture(params=_ENTRIES)
def entry(request: pytest.FixtureRequest, connector: Any, gateway: Any) -> _Entry:
    bridge = KrakenConnectorBridge(api_key="k", secret_key="c2VjcmV0")
    bridge._gateway = gateway
    return _Entry(request.param, connector, bridge)


def _comparable(body: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in body.items() if k not in _UNIQUE}


def _deprecations(caught: list[warnings.WarningMessage]) -> list[warnings.WarningMessage]:
    return [w for w in caught if issubclass(w.category, DeprecationWarning)]


async def _place_legacy(entry: _Entry, case: str) -> None:
    order_type, (price, limit_price), _ = _CASES[case]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        await entry.place(order_type, price, limit_price=limit_price)


async def _place_trigger(entry: _Entry, case: str) -> None:
    order_type, _, (price, limit_price, trigger) = _CASES[case]
    await entry.place(order_type, price, limit_price=limit_price, trigger=trigger)


class TestParity:
    @pytest.mark.parametrize("case", sorted(_CASES))
    async def test_trigger_body_equals_legacy_body(
        self, entry: _Entry, rest: FakeKrakenRest, case: str
    ) -> None:
        await _place_legacy(entry, case)
        await _place_trigger(entry, case)
        legacy, via_trigger = rest.bodies("add_order")
        assert _comparable(via_trigger) == _comparable(legacy)
        assert via_trigger["type"] == entry.side
        assert via_trigger["pair"] == _WIRE_PAIR


class TestNoDeprecationOnTriggerPath:
    @pytest.mark.parametrize("case", sorted(_CASES))
    async def test_trigger_path_is_silent(
        self, entry: _Entry, rest: FakeKrakenRest, case: str
    ) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            await _place_trigger(entry, case)
        assert len(rest.bodies("add_order")) == 1


class TestLegacyStillWorksAndWarns:
    @pytest.mark.parametrize("case", sorted(_CASES))
    async def test_legacy_triggered_form_warns_once_and_sends(
        self, entry: _Entry, rest: FakeKrakenRest, case: str
    ) -> None:
        order_type, (price, limit_price), _ = _CASES[case]
        with pytest.warns(DeprecationWarning, match=_MATCH) as record:
            await entry.place(order_type, price, limit_price=limit_price)
        assert len(_deprecations(list(record))) == 1
        assert len(rest.bodies("add_order")) == 1

    async def test_legacy_percent_form_warns(self, entry: _Entry, rest: FakeKrakenRest) -> None:
        with pytest.warns(DeprecationWarning, match=_MATCH):
            await entry.place(OrderType.STOP_LOSS, _D("2"), price_in_percent=True)
        assert rest.bodies("add_order")[0]["price"] == "#2%"

    async def test_warning_points_at_the_caller(self, entry: _Entry) -> None:
        order_type, (price, limit_price), _ = _CASES["stop_loss"]
        with pytest.warns(DeprecationWarning) as record:
            await entry.place(order_type, price, limit_price=limit_price)
        assert record[0].filename == __file__


class TestValidationParity:
    async def test_percent_trailing_stop_limit_rejected(
        self, entry: _Entry, rest: FakeKrakenRest
    ) -> None:
        trigger = _trail(TriggerKind.PERCENT, "0.02")
        with pytest.raises(UnsupportedOrderError):
            await entry.place(
                OrderType.TRAILING_STOP_LIMIT, limit_price=_D("-0.5"), trigger=trigger
            )
        await asyncio.sleep(0.05)
        assert rest.bodies("add_order") == []

    async def test_trigger_on_limit_order_rejected(
        self, entry: _Entry, rest: FakeKrakenRest
    ) -> None:
        with pytest.raises(ValueError, match="only valid for conditional"):
            await entry.place(OrderType.LIMIT, _D("27000"), trigger=_abs("26000"))
        await asyncio.sleep(0.05)
        assert rest.bodies("add_order") == []

    async def test_trailing_without_trail_rejected(
        self, entry: _Entry, rest: FakeKrakenRest
    ) -> None:
        with pytest.raises(ValueError, match="trigger.trail"):
            await entry.place(OrderType.TRAILING_STOP, trigger=_abs("1"))
        await asyncio.sleep(0.05)
        assert rest.bodies("add_order") == []


class TestNonTriggeredGuard:
    async def test_limit_without_trigger_unchanged_and_silent(
        self, entry: _Entry, rest: FakeKrakenRest
    ) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            await entry.place(OrderType.LIMIT, _D("27000"))
        (body,) = rest.bodies("add_order")
        assert _comparable(body) == {
            "pair": _WIRE_PAIR,
            "type": entry.side,
            "ordertype": "limit",
            "volume": "0.5",
            "price": "27000",
        }


class TestTriggerWithLegacyForm:
    """The gateway rejects (ValueError) a trigger combined with any legacy trigger argument."""

    async def test_trigger_with_price_in_percent_rejected(
        self, entry: _Entry, rest: FakeKrakenRest
    ) -> None:
        with pytest.raises(ValueError, match="price_in_percent"):
            await entry.place(OrderType.STOP_LOSS, price_in_percent=True, trigger=_abs("27000"))
        await asyncio.sleep(0.05)
        assert rest.bodies("add_order") == []

    async def test_trigger_with_legacy_price_rejected(
        self, entry: _Entry, rest: FakeKrakenRest
    ) -> None:
        with pytest.raises(ValueError, match="price must be None"):
            await entry.place(OrderType.STOP_LOSS, _D("27000"), trigger=_abs("27000"))
        await asyncio.sleep(0.05)
        assert rest.bodies("add_order") == []

    async def test_trigger_with_limit_price_on_stop_loss_limit_rejected(
        self, entry: _Entry, rest: FakeKrakenRest
    ) -> None:
        with pytest.raises(ValueError, match="drop limit_price"):
            await entry.place(
                OrderType.STOP_LOSS_LIMIT,
                _D("26950"),
                limit_price=_D("26900"),
                trigger=_abs("27000"),
            )
        await asyncio.sleep(0.05)
        assert rest.bodies("add_order") == []
