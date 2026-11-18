"""Issue #262 RED: int event-id contract on hb-event-bus ``EventKey`` (``str | int``).

Event ids stay ``int`` (hummingbot ``MarketEvent`` values). Subscribers key by the
same int from ``MARKET_EVENT_IDS``; enum members and str forms do NOT match.
"""

from __future__ import annotations

import importlib.metadata
import inspect
import typing
from decimal import Decimal
from typing import Any

import pytest
from data_type_primitives.common import OrderType, TradeType
from data_type_primitives.trade_fee import AddedToCostTradeFee
from packaging.version import Version

from market_connector.exchanges.kraken.hb_compat import kraken_connector_core as core_module
from market_connector.exchanges.kraken.hb_compat.kraken_connector_core import (
    KrakenConnectorCore,
)
from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway
from market_connector.hb_compat import order_events as ev
from market_connector.hb_compat.event_bus import EventBus
from market_connector.hb_compat.order_events import MARKET_EVENT_IDS


def _sample_events() -> dict[str, Any]:
    created = (1.0, OrderType.LIMIT, "BTC-USD", Decimal(1), Decimal(2), "o", 1.0)
    done = (1.0, "o", "BTC", "USD", Decimal(1), Decimal(2), OrderType.LIMIT)
    return {
        "BuyOrderCompletedEvent": ev.BuyOrderCompletedEvent(*done),
        "SellOrderCompletedEvent": ev.SellOrderCompletedEvent(*done),
        "OrderCancelledEvent": ev.OrderCancelledEvent(1.0, "o"),
        "OrderFilledEvent": ev.OrderFilledEvent(
            1.0,
            "o",
            "BTC-USD",
            TradeType.BUY,
            OrderType.LIMIT,
            Decimal(1),
            Decimal(1),
            AddedToCostTradeFee(),
        ),
        "MarketOrderFailureEvent": ev.MarketOrderFailureEvent(1.0, "o", OrderType.LIMIT),
        "BuyOrderCreatedEvent": ev.BuyOrderCreatedEvent(*created),
        "SellOrderCreatedEvent": ev.SellOrderCreatedEvent(*created),
    }


@pytest.fixture
def bus() -> EventBus:
    return EventBus(name="event-key-262")


@pytest.fixture
def core(bus: EventBus) -> KrakenConnectorCore:
    gw = KrakenGateway(api_key="k", secret_key="c2VjcmV0")
    return KrakenConnectorCore(gw, event_bus=bus)


class TestDependencyContract:
    def test_event_bus_is_0_4_and_exports_event_key(self) -> None:
        version = Version(importlib.metadata.version("hb-event-bus"))
        assert Version("0.4") <= version < Version("0.5"), version

        from event_bus import EventKey

        alias = getattr(EventKey, "__value__", EventKey)
        args = typing.get_args(alias)
        assert int in args
        assert str in args


class TestRoundTrip:
    def test_sample_events_cover_every_id(self) -> None:
        assert set(_sample_events()) == set(MARKET_EVENT_IDS)

    @pytest.mark.parametrize("name", sorted(MARKET_EVENT_IDS))
    def test_int_keyed_subscriber_receives_published_event(
        self, core: KrakenConnectorCore, bus: EventBus, name: str
    ) -> None:
        got: list[Any] = []
        bus.subscribe(MARKET_EVENT_IDS[name], got.append)
        event = _sample_events()[name]
        core._emit(event)
        assert got == [event]

    @pytest.mark.parametrize("name", sorted(MARKET_EVENT_IDS))
    def test_str_keyed_subscriber_receives_nothing(
        self, core: KrakenConnectorCore, bus: EventBus, name: str
    ) -> None:
        got: list[Any] = []
        bus.subscribe(str(MARKET_EVENT_IDS[name]), got.append)
        core._emit(_sample_events()[name])
        assert got == []

    @pytest.mark.parametrize("name", sorted(MARKET_EVENT_IDS))
    def test_enum_keyed_subscriber_receives_nothing(
        self, core: KrakenConnectorCore, bus: EventBus, name: str
    ) -> None:
        import enum

        key = MARKET_EVENT_IDS[name]
        member_enum = enum.Enum("MarketEvent", {"X": key})  # plain Enum != int value
        got: list[Any] = []
        bus.subscribe(member_enum.X, got.append)  # type: ignore[arg-type]
        core._emit(_sample_events()[name])
        assert got == []


class TestIdShape:
    def test_ids_are_plain_int_not_bool(self) -> None:
        for value in MARKET_EVENT_IDS.values():
            assert type(value) is int

    def test_ids_are_unique(self) -> None:
        values = list(MARKET_EVENT_IDS.values())
        assert len(set(values)) == len(values)


class TestNoTypeIgnoreOnPublish:
    def test_publish_lines_carry_no_type_ignore(self) -> None:
        lines = inspect.getsource(core_module).splitlines()
        offenders = [
            line.strip() for line in lines if ".publish(" in line and "# type: ignore" in line
        ]
        assert offenders == []
