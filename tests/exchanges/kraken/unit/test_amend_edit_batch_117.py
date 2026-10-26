"""Acceptance tests: Kraken amend, edit and batch endpoints on the per-pair matching pool (#117).

API contract the implementation must provide on ``OrdersMixin``:

- ``amend_order(trading_pair, txid, *, amount=None, price=None) -> str`` returns ``amend_id``.
- ``edit_order(trading_pair, txid, *, amount=None, price=None) -> str`` returns the new txid.
- ``place_orders_batch(trading_pair, orders) -> list[str]``; each order is a Kraken-native
  dict (``type``, ``ordertype``, ``volume``, optional ``price``); 2..15 orders.
- ``cancel_orders_batch(orders) -> int``; ``orders`` is ``[(trading_pair, txid), ...]``, <= 50.

Age comes from ``self._order_clock`` (as in #116); a txid unknown to this process is
charged the worst case.
"""

from __future__ import annotations

import json
import math
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from rate_limit.testing import ManualClock

from market_connector.exchanges.kraken.age_penalties import (
    amend_penalty,
    cancel_penalty,
    edit_penalty,
)
from market_connector.exchanges.kraken.endpoints import ENDPOINT_REGISTRY
from market_connector.exchanges.kraken.mixins.orders import OrdersMixin
from market_connector.exchanges.kraken.specs import KRAKEN_RATE_LIMIT_SPEC
from market_connector.primitives import OrderType, TradeType
from market_connector.rate_limits.limiter import limiter_from_tiered_spec
from market_connector.transport.rest_base import RestConnectorBase

if TYPE_CHECKING:
    from collections.abc import Iterator

    from rate_limit import RateLimiter

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "rest"
PAIR = "XBTUSD"
OTHER_PAIR = "ETHUSD"
CAPACITY = 60  # STARTER matching pool
UNKNOWN_TXID = "OXXXXX-XXXXX-XXXXXX"
NEW_ENDPOINTS = ("amend_order", "edit_order", "add_order_batch", "cancel_order_batch")
_PATHS = {
    "AmendOrder": "amend_order.json",
    "EditOrder": "edit_order.json",
    "CancelOrderBatch": "cancel_order_batch.json",
    "CancelOrder": "cancel_order.json",
}


def _load(name: str) -> dict:
    with (_FIXTURES / name).open() as fh:
        return json.load(fh)


def _body(call: Any) -> dict:
    """The JSON body of a recorded transport call (plain or pre-serialised)."""
    if call.kwargs.get("json") is not None:
        return call.kwargs["json"]
    return json.loads(call.kwargs["content"])


def _respond(*, url: str, **kwargs: Any) -> httpx.Response:
    name = url.rsplit("/", 1)[-1]
    if name == "AddOrderBatch":
        body = kwargs.get("json") or json.loads(kwargs["content"])
        template = _load("add_order_batch.json")
        entry = template["result"]["orders"][0]
        template["result"]["orders"] = [
            {"txid": f"{entry['txid'][:-2]}{i:02d}"} for i in range(len(body["orders"]))
        ]
        return httpx.Response(200, json=template)
    if name == "CancelOrderBatch":
        body = kwargs.get("json") or json.loads(kwargs["content"])
        return httpx.Response(200, json={"error": [], "result": {"count": len(body["orders"])}})
    return httpx.Response(200, json=_load(_PATHS.get(name, "add_order.json")))


class _Orders(OrdersMixin):
    def __init__(self, rest: RestConnectorBase, clock: ManualClock) -> None:
        self._rest = rest
        self._endpoints: dict = {}
        self._order_clock = clock

    @property
    def ready(self) -> bool:
        return True


@pytest.fixture
def order_clock() -> ManualClock:
    """Order-age clock; kept separate so ageing does not refill the limiter's pools."""
    return ManualClock()


@pytest.fixture
def limiter() -> RateLimiter:
    frozen = ManualClock()
    return limiter_from_tiered_spec(
        KRAKEN_RATE_LIMIT_SPEC, "STARTER", clock=frozen, sleep=frozen.sleep
    )


@pytest.fixture
def rest(limiter: RateLimiter) -> RestConnectorBase:
    return RestConnectorBase(
        "https://api.kraken.com", endpoints=ENDPOINT_REGISTRY, rate_limiter=limiter
    )


@pytest.fixture
def send(rest: RestConnectorBase) -> Iterator[AsyncMock]:
    with patch.object(rest, "_client") as mock_client:
        mock_client.request = AsyncMock(side_effect=_respond)
        yield mock_client.request


@pytest.fixture
def orders(rest: RestConnectorBase, order_clock: ManualClock, send: AsyncMock) -> _Orders:
    return _Orders(rest, order_clock)


def _remaining(limiter: RateLimiter, pair: str) -> int:
    """Units the pair's matching pool still admits (consumes them)."""
    pool = f"matching:{pair}"
    count = 0
    while limiter.try_acquire(pool, {pool: 1}) == 0:
        count += 1
    return count


async def _place(orders: _Orders, pair: str = PAIR) -> str:
    return await orders.place_order(pair, OrderType.MARKET, TradeType.BUY, Decimal("1"))


def _batch(n: int) -> list[dict[str, str]]:
    return [
        {"type": "buy", "ordertype": "limit", "volume": "1", "price": f"{30000 + i}"}
        for i in range(n)
    ]


def _calls_to(send: AsyncMock, suffix: str) -> list[Any]:
    return [c for c in send.call_args_list if c.kwargs["url"].endswith(suffix)]


class TestRegistry:
    """AC1: new endpoints are registered and charged per pair on the matching pool."""

    @pytest.mark.parametrize(
        ("name", "path"),
        [
            ("amend_order", "/0/private/AmendOrder"),
            ("edit_order", "/0/private/EditOrder"),
            ("add_order_batch", "/0/private/AddOrderBatch"),
            ("cancel_order_batch", "/0/private/CancelOrderBatch"),
        ],
    )
    def test_endpoint_registered(self, name: str, path: str) -> None:
        assert name in ENDPOINT_REGISTRY
        assert ENDPOINT_REGISTRY[name].path == path
        assert ENDPOINT_REGISTRY[name].method == "POST"

    @pytest.mark.parametrize("name", NEW_ENDPOINTS)
    def test_keyed_matching_pool_declared(self, name: str) -> None:
        assert KRAKEN_RATE_LIMIT_SPEC.keyed_endpoint_pools.get(name) == [("matching", 1)]
        assert name not in KRAKEN_RATE_LIMIT_SPEC.endpoint_pools

    def test_every_registry_endpoint_is_in_exactly_one_pool_table(self) -> None:
        static = set(KRAKEN_RATE_LIMIT_SPEC.endpoint_pools)
        keyed = set(KRAKEN_RATE_LIMIT_SPEC.keyed_endpoint_pools)
        assert static | keyed == set(ENDPOINT_REGISTRY)
        assert static.isdisjoint(keyed)


class TestAmend:
    async def test_amend_at_age_2s_consumes_4_and_returns_amend_id(
        self, orders: _Orders, limiter: RateLimiter, order_clock: ManualClock
    ) -> None:
        txid = await _place(orders)  # 1
        order_clock.advance(2.0)

        amend_id = await orders.amend_order(PAIR, txid, amount=Decimal("2"), price=Decimal("1"))

        assert amend_id == "TAAAAA-AAAAA-AAAAAA"
        assert _remaining(limiter, PAIR) == CAPACITY - 1 - amend_penalty(2.0)

    async def test_amend_body(self, orders: _Orders, send: AsyncMock) -> None:
        txid = await _place(orders)

        await orders.amend_order(PAIR, txid, amount=Decimal("2"), price=Decimal("30500"))

        body = _body(_calls_to(send, "AmendOrder")[0])
        assert body["txid"] == txid
        assert Decimal(str(body["order_qty"])) == Decimal("2")
        assert Decimal(str(body["limit_price"])) == Decimal("30500")

    async def test_amend_omits_unset_fields(self, orders: _Orders, send: AsyncMock) -> None:
        txid = await _place(orders)

        await orders.amend_order(PAIR, txid, price=Decimal("30500"))

        body = _body(_calls_to(send, "AmendOrder")[0])
        assert "order_qty" not in body
        assert "limit_price" in body

    async def test_amend_resets_age_to_now(
        self, orders: _Orders, limiter: RateLimiter, order_clock: ManualClock
    ) -> None:
        """A txid keeps its id on amend; age restarts, so a later cancel is charged young."""
        txid = await _place(orders)
        order_clock.advance(400.0)
        await orders.amend_order(PAIR, txid, price=Decimal("1"))  # age 400 -> 1
        order_clock.advance(2.0)

        await orders.cancel_order(PAIR, txid)

        assert _remaining(limiter, PAIR) == CAPACITY - 1 - 1 - cancel_penalty(2.0)

    async def test_unknown_txid_charges_worst_case_4(
        self, orders: _Orders, limiter: RateLimiter
    ) -> None:
        await orders.amend_order(PAIR, UNKNOWN_TXID, price=Decimal("1"))

        assert _remaining(limiter, PAIR) == CAPACITY - amend_penalty(0)
        assert amend_penalty(0) == 4

    async def test_other_pair_unaffected(self, orders: _Orders, limiter: RateLimiter) -> None:
        await orders.amend_order(PAIR, UNKNOWN_TXID, price=Decimal("1"))

        assert _remaining(limiter, OTHER_PAIR) == CAPACITY


class TestEdit:
    async def test_edit_at_age_2s_consumes_7_and_returns_new_txid(
        self, orders: _Orders, limiter: RateLimiter, order_clock: ManualClock
    ) -> None:
        txid = await _place(orders)
        order_clock.advance(2.0)

        new_txid = await orders.edit_order(PAIR, txid, amount=Decimal("2"), price=Decimal("1"))

        assert new_txid == "ONEWED-NEWED-NEWEDI"
        assert _remaining(limiter, PAIR) == CAPACITY - 1 - edit_penalty(2.0)

    async def test_edit_body(self, orders: _Orders, send: AsyncMock) -> None:
        txid = await _place(orders)

        await orders.edit_order(PAIR, txid, amount=Decimal("2"), price=Decimal("30500"))

        body = _body(_calls_to(send, "EditOrder")[0])
        assert body["txid"] == txid
        assert body["pair"] == PAIR
        assert Decimal(str(body["volume"])) == Decimal("2")
        assert Decimal(str(body["price"])) == Decimal("30500")

    async def test_edit_omits_unset_fields(self, orders: _Orders, send: AsyncMock) -> None:
        txid = await _place(orders)

        await orders.edit_order(PAIR, txid, price=Decimal("30500"))

        body = _body(_calls_to(send, "EditOrder")[0])
        assert "volume" not in body

    async def test_unknown_txid_charges_worst_case_7(
        self, orders: _Orders, limiter: RateLimiter
    ) -> None:
        await orders.edit_order(PAIR, UNKNOWN_TXID, price=Decimal("1"))

        assert _remaining(limiter, PAIR) == CAPACITY - 7

    async def test_old_txid_forgotten_new_txid_aged_from_now(
        self, orders: _Orders, limiter: RateLimiter, order_clock: ManualClock
    ) -> None:
        txid = await _place(orders)
        order_clock.advance(400.0)
        new_txid = await orders.edit_order(PAIR, txid, price=Decimal("1"))  # age 400 -> 1
        order_clock.advance(2.0)

        await orders.cancel_order(PAIR, new_txid)  # young: 8
        await orders.cancel_order(PAIR, txid)  # forgotten: unknown 8

        assert _remaining(limiter, PAIR) == CAPACITY - 1 - 1 - 8 - 8


class TestPlaceOrdersBatch:
    async def test_returns_txids_in_order(self, orders: _Orders) -> None:
        txids = await orders.place_orders_batch(PAIR, _batch(3))

        assert len(txids) == 3
        assert len(set(txids)) == 3
        assert all(isinstance(t, str) for t in txids)

    async def test_body_carries_pair_and_orders(self, orders: _Orders, send: AsyncMock) -> None:
        await orders.place_orders_batch(PAIR, _batch(2))

        body = _body(_calls_to(send, "AddOrderBatch")[0])
        assert body["pair"] == PAIR
        assert len(body["orders"]) == 2
        assert body["orders"][0]["ordertype"] == "limit"

    @pytest.mark.parametrize("n", [2, 3, 4, 7, 14, 15])
    async def test_charges_ceil_half(self, orders: _Orders, limiter: RateLimiter, n: int) -> None:
        await orders.place_orders_batch(PAIR, _batch(n))

        assert _remaining(limiter, PAIR) == CAPACITY - math.ceil(n / 2)

    async def test_other_pair_unaffected(self, orders: _Orders, limiter: RateLimiter) -> None:
        await orders.place_orders_batch(PAIR, _batch(4))

        assert _remaining(limiter, OTHER_PAIR) == CAPACITY

    @pytest.mark.parametrize("n", [0, 1, 16, 30])
    async def test_size_out_of_range_rejected_before_send(
        self, orders: _Orders, send: AsyncMock, n: int
    ) -> None:
        with pytest.raises(ValueError, match=r"2|15"):
            await orders.place_orders_batch(PAIR, _batch(n))

        send.assert_not_called()

    async def test_records_placement_time_of_each_txid(
        self, orders: _Orders, limiter: RateLimiter, order_clock: ManualClock
    ) -> None:
        order_clock.advance(1000.0)
        txids = await orders.place_orders_batch(PAIR, _batch(2))  # 1
        order_clock.advance(100.0)

        for txid in txids:
            await orders.cancel_order(PAIR, txid)  # 100s old: 1 each

        assert _remaining(limiter, PAIR) == CAPACITY - 1 - 2 * cancel_penalty(100.0)


class TestCancelOrdersBatch:
    async def test_sums_penalties_per_pair_and_returns_count(
        self, orders: _Orders, limiter: RateLimiter, order_clock: ManualClock
    ) -> None:
        old = await _place(orders)  # 1
        order_clock.advance(400.0)
        young = await _place(orders)  # 1
        order_clock.advance(2.0)

        count = await orders.cancel_orders_batch([(PAIR, old), (PAIR, young)])

        assert count == 2
        assert _remaining(limiter, PAIR) == CAPACITY - 2 - (cancel_penalty(402.0) + 8)

    async def test_charges_each_pair_separately(
        self, orders: _Orders, limiter: RateLimiter, order_clock: ManualClock
    ) -> None:
        btc = await _place(orders, PAIR)
        eth = await _place(orders, OTHER_PAIR)
        order_clock.advance(2.0)

        await orders.cancel_orders_batch([(PAIR, btc), (OTHER_PAIR, eth)])

        assert _remaining(limiter, PAIR) == CAPACITY - 1 - 8
        assert _remaining(limiter, OTHER_PAIR) == CAPACITY - 1 - 8

    async def test_unknown_txids_charge_worst_case_each(
        self, orders: _Orders, limiter: RateLimiter
    ) -> None:
        await orders.cancel_orders_batch([(PAIR, UNKNOWN_TXID), (PAIR, UNKNOWN_TXID + "2")])

        assert _remaining(limiter, PAIR) == CAPACITY - 16

    async def test_pair_with_zero_sum_not_charged_but_request_sent(
        self, orders: _Orders, limiter: RateLimiter, order_clock: ManualClock, send: AsyncMock
    ) -> None:
        eth = await _place(orders, OTHER_PAIR)
        btc = await _place(orders, PAIR)
        order_clock.advance(400.0)

        count = await orders.cancel_orders_batch([(OTHER_PAIR, eth), (PAIR, btc)])

        assert count == 2
        assert len(_calls_to(send, "CancelOrderBatch")) == 1
        assert _remaining(limiter, OTHER_PAIR) == CAPACITY - 1
        assert _remaining(limiter, PAIR) == CAPACITY - 1

    async def test_all_zero_sum_still_sent(
        self, orders: _Orders, limiter: RateLimiter, order_clock: ManualClock, send: AsyncMock
    ) -> None:
        txid = await _place(orders)
        order_clock.advance(400.0)

        await orders.cancel_orders_batch([(PAIR, txid)])

        assert len(_calls_to(send, "CancelOrderBatch")) == 1
        assert _remaining(limiter, PAIR) == CAPACITY - 1

    async def test_body_lists_txids(self, orders: _Orders, send: AsyncMock) -> None:
        await orders.cancel_orders_batch([(PAIR, "OAAAAA-AAAAA-AAAAAA"), (PAIR, UNKNOWN_TXID)])

        body = _body(_calls_to(send, "CancelOrderBatch")[0])
        assert body["orders"] == ["OAAAAA-AAAAA-AAAAAA", UNKNOWN_TXID]

    async def test_cancelled_txids_forgotten(
        self, orders: _Orders, limiter: RateLimiter, order_clock: ManualClock
    ) -> None:
        txid = await _place(orders)
        order_clock.advance(400.0)
        await orders.cancel_orders_batch([(PAIR, txid)])  # free

        await orders.cancel_order(PAIR, txid)  # unknown now: 8

        assert _remaining(limiter, PAIR) == CAPACITY - 1 - 8

    async def test_max_50_accepted(self, orders: _Orders, send: AsyncMock) -> None:
        batch = [(f"PAIR{i}", f"OTX{i:03d}-AAAAA-AAAAAA") for i in range(50)]

        assert await orders.cancel_orders_batch(batch) == 50
        assert len(_calls_to(send, "CancelOrderBatch")) == 1

    async def test_over_50_rejected_before_send(self, orders: _Orders, send: AsyncMock) -> None:
        batch = [(PAIR, f"OTX{i:03d}-AAAAA-AAAAAA") for i in range(51)]

        with pytest.raises(ValueError, match="50"):
            await orders.cancel_orders_batch(batch)

        send.assert_not_called()
