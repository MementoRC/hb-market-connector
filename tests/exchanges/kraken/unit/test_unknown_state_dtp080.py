"""A lost Kraken AddOrder leaves the order in upstream ``OrderState.UNKNOWN`` (dtp v0.8.0, #214)."""

from __future__ import annotations

from typing import Any

from data_type_primitives.in_flight_order import InFlightOrder, OrderState

from tests.exchanges.kraken.unit import test_kraken_connector_core_211 as base
from tests.exchanges.kraken.unit import test_userref_recovery_214 as recovery

# Fixtures reused as-is (pytest discovers them by module attribute).
bare_connector, bus, connector = base.bare_connector, base.bus, base.connector
gateway, seen = base.gateway, base.seen
rest = recovery.rest


async def test_lost_add_order_is_upstream_unknown(connector: Any, rest: Any) -> None:
    cid = await recovery._lose_one(connector, rest, recovery._LOST[0], lost_as=None)  # noqa: SLF001

    order = connector.fetch_order(cid)
    assert order.current_state is OrderState.UNKNOWN
    assert order.is_open
    assert not order.is_done

    restored = InFlightOrder.from_json(order.to_json())
    assert restored.current_state is OrderState.UNKNOWN
    assert restored.is_open
