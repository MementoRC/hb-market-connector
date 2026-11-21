"""Acceptance tests for #300: Coinbase cancel_order must send the exchange order id."""

import asyncio
from decimal import Decimal
from typing import Any

import pytest

from market_connector.exceptions import OrderNotFoundError, OrderRejectedError
from market_connector.exchanges.coinbase.mixins import orders as orders_module
from market_connector.exchanges.coinbase.mixins.orders import OrdersMixin, _placing
from market_connector.primitives import OrderType, TradeType
from market_connector.testing.mock_transport import MockRestClient


class _RecordingRest(MockRestClient):
    """MockRestClient that records calls and serves list_orders pages by cursor."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, dict[str, Any] | None, dict[str, Any] | None]] = []
        self.pages: dict[str | None, dict[str, Any]] = {}

    async def request(self, endpoint_name, params=None, data=None, headers=None):
        self.calls.append((endpoint_name, params, data))
        if endpoint_name == "list_orders" and self.pages:
            self._responses["list_orders"] = self.pages[(params or {}).get("cursor")]
        return await super().request(endpoint_name, params=params, data=data, headers=headers)

    def cancel_bodies(self) -> list[dict[str, Any] | None]:
        return [d for name, _, d in self.calls if name == "cancel_orders"]


class _Gateway(OrdersMixin):
    def __init__(self, rest: _RecordingRest) -> None:
        self._rest = rest
        self._endpoints = {}
        self._started = True

    @property
    def ready(self) -> bool:
        return self._started


def _order(order_id: str, client_order_id: str) -> dict[str, Any]:
    return {
        "order_id": order_id,
        "client_order_id": client_order_id,
        "product_id": "BTC-USD",
        "side": "BUY",
        "status": "OPEN",
        "order_configuration": {"limit_limit_gtc": {"base_size": "0.5", "limit_price": "50000"}},
        "filled_size": "0",
        "average_filled_price": "0",
    }


def _cancel_result(order_id: str, *, success: bool = True) -> dict[str, Any]:
    return {
        "results": [
            {
                "success": success,
                "order_id": order_id,
                "failure_reason": None if success else "UNKNOWN_CANCEL_FAILURE_REASON",
            }
        ]
    }


async def _place(rest: _RecordingRest, gateway: _Gateway, exchange_id: str) -> str:
    rest.register(
        "place_order",
        {
            "success": True,
            "order_id": exchange_id,
            "success_response": {
                "order_id": exchange_id,
                "product_id": "BTC-USD",
                "side": "BUY",
                "client_order_id": "ignored",
            },
        },
    )
    return await gateway.place_order(
        "BTC-USD", OrderType.LIMIT, TradeType.BUY, Decimal("0.5"), Decimal("50000")
    )


async def test_cancel_after_place_sends_exchange_order_id():
    rest = _RecordingRest()
    gateway = _Gateway(rest)
    client_id = await _place(rest, gateway, "X")
    rest.register("cancel_orders", _cancel_result("X"))

    assert await gateway.cancel_order("BTC-USD", client_id) is True
    assert rest.cancel_bodies() == [{"order_ids": ["X"]}]


async def test_cancel_on_fresh_gateway_resolves_exchange_id_via_lookup():
    rest = _RecordingRest()
    rest.register("list_orders", {"orders": [_order("X", "C")]})
    rest.register("cancel_orders", _cancel_result("X"))
    gateway = _Gateway(rest)

    assert await gateway.cancel_order("BTC-USD", "C") is True
    assert rest.cancel_bodies() == [{"order_ids": ["X"]}]
    assert any(name == "list_orders" for name, _, _ in rest.calls)


async def test_cancel_on_fresh_gateway_resolves_across_two_pages():
    rest = _RecordingRest()
    rest.pages = {
        None: {"orders": [_order("Y", "other")], "has_next": True, "cursor": "p2"},
        "p2": {"orders": [_order("X", "C")], "has_next": False},
    }
    rest.register("cancel_orders", _cancel_result("X"))
    gateway = _Gateway(rest)

    assert await gateway.cancel_order("BTC-USD", "C") is True
    assert rest.cancel_bodies() == [{"order_ids": ["X"]}]


async def test_cancel_unresolvable_client_id_raises_order_not_found_and_sends_no_cancel():
    rest = _RecordingRest()
    rest.register("list_orders", {"orders": [_order("Y", "other")]})
    rest.register("cancel_orders", _cancel_result("Y"))
    gateway = _Gateway(rest)

    with pytest.raises(OrderNotFoundError):
        await gateway.cancel_order("BTC-USD", "C")
    assert rest.cancel_bodies() == []


async def test_cancel_mismatched_pair_raises_and_sends_no_cancel_on_lookup_path():
    rest = _RecordingRest()
    rest.register("list_orders", {"orders": [_order("X", "C")]})
    rest.register("cancel_orders", _cancel_result("X"))
    gateway = _Gateway(rest)

    with pytest.raises(OrderNotFoundError):
        await gateway.cancel_order("ETH-USD", "C")
    assert rest.cancel_bodies() == []


async def test_cancel_mismatched_pair_raises_on_cached_path():
    rest = _RecordingRest()
    gateway = _Gateway(rest)
    client_id = await _place(rest, gateway, "X")
    rest.register("cancel_orders", _cancel_result("X"))

    with pytest.raises(OrderNotFoundError):
        await gateway.cancel_order("ETH-USD", client_id)
    assert rest.cancel_bodies() == []


async def test_place_uses_success_response_order_id_when_top_level_absent():
    rest = _RecordingRest()
    gateway = _Gateway(rest)
    rest.register(
        "place_order",
        {
            "success": True,
            "success_response": {
                "order_id": "X",
                "product_id": "BTC-USD",
                "side": "BUY",
                "client_order_id": "ignored",
            },
        },
    )
    client_id = await gateway.place_order(
        "BTC-USD", OrderType.LIMIT, TradeType.BUY, Decimal("0.5"), Decimal("50000")
    )
    rest.register("cancel_orders", _cancel_result("X"))

    assert await gateway.cancel_order("BTC-USD", client_id) is True
    assert rest.cancel_bodies() == [{"order_ids": ["X"]}]
    assert not any(name == "list_orders" for name, _, _ in rest.calls)


async def test_cancel_waits_for_in_flight_placement():
    rest = _RecordingRest()
    gate = asyncio.Event()
    original = rest.request

    async def slow_request(endpoint_name, params=None, data=None, headers=None):
        if endpoint_name == "place_order":
            await gate.wait()
        return await original(endpoint_name, params=params, data=data, headers=headers)

    rest.request = slow_request  # type: ignore[method-assign]
    gateway = _Gateway(rest)
    rest.register(
        "place_order",
        {"success": True, "order_id": "X", "success_response": None},
    )
    rest.register("cancel_orders", _cancel_result("X"))

    place = asyncio.create_task(
        gateway.place_order(
            "BTC-USD",
            OrderType.LIMIT,
            TradeType.BUY,
            Decimal("0.5"),
            Decimal("50000"),
            client_order_id="C",
        )
    )
    await asyncio.sleep(0)
    cancel = asyncio.create_task(gateway.cancel_order("BTC-USD", "C"))
    await asyncio.sleep(0)
    gate.set()
    await place

    assert await cancel is True
    assert rest.cancel_bodies() == [{"order_ids": ["X"]}]


class _GatedGateway:
    """Gateway whose place_order POSTs block on per-call gates; behaviour set per call."""

    def __init__(self) -> None:
        self.rest = _RecordingRest()
        self.gateway = _Gateway(self.rest)
        self.gates: list[asyncio.Event] = []
        self.failures: list[BaseException | None] = []
        original = self.rest.request

        async def request(endpoint_name, params=None, data=None, headers=None):
            if endpoint_name == "place_order":
                idx = len([1 for n, _, _ in self.rest.calls if n == "place_order"])
                self.rest.calls.append((endpoint_name, params, data))
                await self.gates[idx].wait()
                failure = self.failures[idx]
                if failure is not None:
                    raise failure
                return await MockRestClient.request(
                    self.rest, endpoint_name, params=params, data=data, headers=headers
                )
            return await original(endpoint_name, params=params, data=data, headers=headers)

        self.rest.request = request  # type: ignore[method-assign]
        self.rest.register("list_orders", {"orders": []})
        self.rest.register("cancel_orders", _cancel_result("X"))

    def start_place(self, failure: BaseException | None = None) -> asyncio.Task[str]:
        self.gates.append(asyncio.Event())
        self.failures.append(failure)
        return asyncio.create_task(
            self.gateway.place_order(
                "BTC-USD",
                OrderType.LIMIT,
                TradeType.BUY,
                Decimal("0.5"),
                Decimal("50000"),
                client_order_id="C",
            )
        )


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_cancel_released_when_placement_rejected():
    h = _GatedGateway()
    h.rest.register("place_order", {"success": False, "failure_reason": "INSUFFICIENT_FUND"})
    place = h.start_place()
    await _settle()
    cancel = asyncio.create_task(h.gateway.cancel_order("BTC-USD", "C"))
    await _settle()
    assert not cancel.done()
    h.gates[0].set()
    with pytest.raises(OrderRejectedError):
        await place
    with pytest.raises(OrderNotFoundError):
        await asyncio.wait_for(cancel, timeout=2)
    assert h.rest.cancel_bodies() == []


async def test_cancel_released_when_placement_raises():
    h = _GatedGateway()
    place = h.start_place(failure=RuntimeError("boom"))
    await _settle()
    cancel = asyncio.create_task(h.gateway.cancel_order("BTC-USD", "C"))
    await _settle()
    assert not cancel.done()
    h.gates[0].set()
    with pytest.raises(RuntimeError):
        await place
    with pytest.raises(OrderNotFoundError):
        await asyncio.wait_for(cancel, timeout=2)
    assert h.rest.cancel_bodies() == []


async def test_cancel_released_when_placement_task_cancelled():
    h = _GatedGateway()
    place = h.start_place()
    await _settle()
    cancel = asyncio.create_task(h.gateway.cancel_order("BTC-USD", "C"))
    await _settle()
    assert not cancel.done()
    place.cancel()
    with pytest.raises(asyncio.CancelledError):
        await place
    with pytest.raises(OrderNotFoundError):
        await asyncio.wait_for(cancel, timeout=2)
    assert h.rest.cancel_bodies() == []
    assert "C" not in _placing(h.gateway)


async def test_same_client_id_concurrent_placements_do_not_orphan_event():
    h = _GatedGateway()
    h.rest.register("place_order", {"success": True, "order_id": "X", "success_response": None})
    first = h.start_place()
    await _settle()
    second = h.start_place()
    await _settle()
    second_event = _placing(h.gateway)["C"]

    h.gates[0].set()
    await first
    # First finisher must not pop or leave the second call's event unreachable.
    assert _placing(h.gateway).get("C") is second_event
    assert not second_event.is_set()

    cancel = asyncio.create_task(h.gateway.cancel_order("BTC-USD", "C"))
    await _settle()
    assert not cancel.done()
    h.gates[1].set()
    await second
    assert await asyncio.wait_for(cancel, timeout=2) is True
    assert h.rest.cancel_bodies() == [{"order_ids": ["X"]}]
    assert "C" not in _placing(h.gateway)


async def test_cancel_wait_timeout_falls_through_to_normal_resolution(monkeypatch):
    monkeypatch.setattr(orders_module, "_PLACEMENT_WAIT_TIMEOUT_S", 0.05)
    h = _GatedGateway()
    h.rest.register("list_orders", {"orders": [_order("X", "C")]})
    place = h.start_place()
    await _settle()

    # Placement never resolves: cancel must not hang, and falls back to the lookup.
    assert await asyncio.wait_for(h.gateway.cancel_order("BTC-USD", "C"), timeout=2) is True
    assert h.rest.cancel_bodies() == [{"order_ids": ["X"]}]

    place.cancel()
    with pytest.raises(asyncio.CancelledError):
        await place


async def test_cancel_wait_timeout_unresolvable_raises_order_not_found(monkeypatch):
    monkeypatch.setattr(orders_module, "_PLACEMENT_WAIT_TIMEOUT_S", 0.05)
    h = _GatedGateway()
    place = h.start_place()
    await _settle()

    with pytest.raises(OrderNotFoundError):
        await asyncio.wait_for(h.gateway.cancel_order("BTC-USD", "C"), timeout=2)

    place.cancel()
    with pytest.raises(asyncio.CancelledError):
        await place


async def test_cancel_resolves_stop_limit_order_without_conversion():
    rest = _RecordingRest()
    stop = _order("X", "C")
    stop["order_configuration"] = {
        "stop_limit_stop_limit_gtc": {
            "base_size": "0.5",
            "limit_price": "50000",
            "stop_price": "49000",
            "stop_direction": "STOP_DIRECTION_STOP_DOWN",
        }
    }
    rest.register("list_orders", {"orders": [stop]})
    rest.register("cancel_orders", _cancel_result("X"))
    gateway = _Gateway(rest)

    assert await gateway.cancel_order("BTC-USD", "C") is True
    assert rest.cancel_bodies() == [{"order_ids": ["X"]}]


async def test_lookup_of_terminal_order_drops_cached_id():
    from market_connector.exchanges.coinbase.mixins.orders import _exchange_ids

    rest = _RecordingRest()
    gateway = _Gateway(rest)
    client_id = await _place(rest, gateway, "X")
    filled = _order("X", client_id)
    filled["status"] = "FILLED"
    rest.register("list_orders", {"orders": [filled]})

    await gateway.get_order_by_client_order_id(client_id)
    assert client_id not in _exchange_ids(gateway)


async def test_cancel_reported_failure_returns_false():
    rest = _RecordingRest()
    gateway = _Gateway(rest)
    client_id = await _place(rest, gateway, "X")
    rest.register("cancel_orders", _cancel_result("X", success=False))

    assert await gateway.cancel_order("BTC-USD", client_id) is False
    assert rest.cancel_bodies() == [{"order_ids": ["X"]}]
