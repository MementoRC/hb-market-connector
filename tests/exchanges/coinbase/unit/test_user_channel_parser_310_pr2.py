"""Acceptance tests for #310 PR 2: Coinbase ``user`` channel parser (RED step).

Assumed public API (does not exist yet)::

    market_connector.exchanges.coinbase.user_channel.parse_user_message(
        msg: dict[str, Any],
    ) -> UserChannelMessage

``UserChannelMessage`` exposes ``channel: str``, ``timestamp: datetime`` (timezone-aware),
``sequence_num: int`` and ``events: list[UserEvent]`` (the existing ``schemas.ws`` models).

Fixtures are built from the documented schema (the docs publish no sample frames). Every
numeric field is a string on the wire and must stay a string in ``UserOrder``.
"""

from __future__ import annotations

from typing import Any

import pytest

from market_connector.exchanges.coinbase.schemas.ws import UserEvent, UserOrder

STATUSES = ["PENDING", "OPEN", "FILLED", "CANCEL_QUEUED", "CANCELLED", "EXPIRED", "FAILED"]


def make_order(**overrides: Any) -> dict[str, Any]:
    order: dict[str, Any] = {
        "order_id": "order-001",
        "client_order_id": "client-001",
        "product_id": "BTC-USD",
        "cumulative_quantity": "0.005",
        "leaves_quantity": "0.005",
        "avg_price": "50000.00",
        "number_of_fills": "1",
        "filled_value": "250.00",
        "total_fees": "0.025",
        "status": "OPEN",
        "creation_time": "2026-04-24T11:00:00Z",
        "order_side": "BUY",
        "order_type": "Limit",
        "limit_price": "50000.00",
        "stop_price": "0",
    }
    order.update(overrides)
    return order


def make_envelope(
    events: list[dict[str, Any]],
    sequence_num: int = 1,
    channel: str = "user",
    timestamp: str = "2026-04-24T12:00:00.123456789Z",
) -> dict[str, Any]:
    return {
        "channel": channel,
        "client_id": "",
        "timestamp": timestamp,
        "sequence_num": sequence_num,
        "events": events,
    }


def parse(msg: dict[str, Any]) -> Any:
    from market_connector.exchanges.coinbase.user_channel import parse_user_message

    return parse_user_message(msg)


def test_envelope_fields_are_parsed() -> None:
    parsed = parse(make_envelope([{"type": "snapshot", "orders": [make_order()]}], sequence_num=7))

    assert parsed.channel == "user"
    assert parsed.sequence_num == 7
    assert parsed.timestamp.tzinfo is not None
    assert (parsed.timestamp.year, parsed.timestamp.month, parsed.timestamp.day) == (2026, 4, 24)
    assert (parsed.timestamp.hour, parsed.timestamp.minute) == (12, 0)


def test_events_are_user_events_with_user_orders() -> None:
    parsed = parse(
        make_envelope(
            [
                {
                    "type": "snapshot",
                    "orders": [make_order(order_id="a"), make_order(order_id="b")],
                },
                {"type": "update", "orders": [make_order(order_id="c")]},
            ]
        )
    )

    assert [e.type for e in parsed.events] == ["snapshot", "update"]
    assert all(isinstance(e, UserEvent) for e in parsed.events)
    assert [o.order_id for o in parsed.events[0].orders] == ["a", "b"]
    assert all(isinstance(o, UserOrder) for e in parsed.events for o in e.orders)
    assert parsed.events[1].orders[0].order_id == "c"


def test_numeric_fields_stay_strings() -> None:
    order = parse(make_envelope([{"type": "update", "orders": [make_order()]}])).events[0].orders[0]

    assert order.cumulative_quantity == "0.005"
    assert order.leaves_quantity == "0.005"
    assert order.avg_price == "50000.00"
    assert order.total_fees == "0.025"


@pytest.mark.parametrize("status", STATUSES)
def test_every_documented_status_parses(status: str) -> None:
    parsed = parse(make_envelope([{"type": "update", "orders": [make_order(status=status)]}]))

    assert parsed.events[0].orders[0].status == status


def test_leaves_quantity_is_not_assumed_to_be_base_units() -> None:
    # A quote-sized market buy: leaves_quantity is in USD (250.00), far above the base-unit
    # cumulative_quantity. The parser must neither reject nor "correct" it.
    order = make_order(
        cumulative_quantity="0.001", leaves_quantity="250.00", order_type="Market", limit_price="0"
    )

    parsed = parse(make_envelope([{"type": "update", "orders": [order]}])).events[0].orders[0]

    assert parsed.cumulative_quantity == "0.001"
    assert parsed.leaves_quantity == "250.00"


def test_zero_limit_and_stop_prices_do_not_break_parsing() -> None:
    # "0" means not-applicable for limit_price/stop_price (market orders etc.).
    order = make_order(limit_price="0", stop_price="0", order_type="Market")

    parsed = parse(make_envelope([{"type": "update", "orders": [order]}]))

    assert parsed.events[0].orders[0].order_id == "order-001"


@pytest.mark.parametrize("variant", ["missing", "empty"])
def test_missing_or_empty_client_order_id_is_tolerated(variant: str) -> None:
    # Whether every event echoes client_order_id is unverified (needs live probe, #310).
    order = make_order()
    if variant == "missing":
        del order["client_order_id"]
    else:
        order["client_order_id"] = ""

    parsed = parse(make_envelope([{"type": "update", "orders": [order]}]))

    assert parsed.events[0].orders[0].order_id == "order-001"
    assert not parsed.events[0].orders[0].client_order_id


def test_empty_snapshot_parses() -> None:
    parsed = parse(make_envelope([{"type": "snapshot", "orders": []}]))

    assert parsed.events[0].type == "snapshot"
    assert parsed.events[0].orders == []
