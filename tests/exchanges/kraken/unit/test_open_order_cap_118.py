"""Acceptance tests: Kraken per-pair max open orders cap with resync at the cap (#118).

API contract the implementation must provide:

- ``specs.KRAKEN_MAX_OPEN_ORDERS_PER_PAIR: Mapping[KrakenAPITier, int]`` (60 / 80 / 225).
- ``OrdersMixin`` reads the cap for ``self._config.tier`` (a ``KrakenConfig``, as on
  ``KrakenGateway``); the stub below sets ``_config`` accordingly.
- ``errors.OpenOrdersLimitError`` (subclass of ``OrderRejectedError``), mapped from
  ``"EOrder:Orders limit exceeded"`` in ``ERROR_CODE_MAPPING``.
- The local per-pair count is private state; these tests observe it only through
  behaviour: ``get_open_orders`` (resync) is called exactly when ``count + n > cap``.
  Open orders are matched to a pair by ``OrderInfo.descr.pair`` (Kraken altname).
"""

from __future__ import annotations

import itertools
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from market_connector.exceptions import OrderRejectedError
from market_connector.exchanges.kraken import errors, specs
from market_connector.exchanges.kraken.errors import (
    ERROR_CODE_MAPPING,
    OpenOrdersLimitError,
    raise_on_kraken_error,
)
from market_connector.exchanges.kraken.kraken_gateway import KrakenConfig
from market_connector.exchanges.kraken.mixins.orders import OrdersMixin
from market_connector.exchanges.kraken.schemas.enums import KrakenAPITier
from market_connector.primitives import OrderType, TradeType

PAIR = "XBTUSD"
OTHER_PAIR = "ETHUSD"
CAP = 60  # STARTER
LIMIT_ERROR = "EOrder:Orders limit exceeded"


class _FakeRest:
    """Kraken REST double: records endpoint names; ``server_open`` backs OpenOrders."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.server_open: list[tuple[str, str]] = []  # (txid, altname pair)
        self.reject_add: list[str] = []
        self._seq = itertools.count()

    def n(self, endpoint: str) -> int:
        return self.calls.count(endpoint)

    def _txid(self) -> str:
        return f"O{next(self._seq):05d}-AAAAA-AAAAAA"

    async def request(self, endpoint: str, data: dict | None = None, **_: Any) -> Any:
        self.calls.append(endpoint)
        data = data or {}
        return SimpleNamespace(raw=self._answer(endpoint, data))

    def _answer(self, endpoint: str, data: dict) -> dict[str, Any]:
        if endpoint in ("add_order", "add_order_batch") and self.reject_add:
            return {"error": self.reject_add, "result": {}}
        if endpoint == "add_order":
            return {"error": [], "result": {"txid": [self._txid()], "descr": {"order": "x"}}}
        if endpoint == "add_order_batch":
            return {
                "error": [],
                "result": {"orders": [{"txid": self._txid()} for _ in data["orders"]]},
            }
        if endpoint == "cancel_order":
            return {"error": [], "result": {"count": 1}}
        if endpoint == "cancel_order_batch":
            return {"error": [], "result": {"count": len(data["orders"])}}
        if endpoint == "edit_order":
            return {"error": [], "result": {"txid": self._txid()}}
        if endpoint == "open_orders":
            open_ = {
                txid: {"status": "open", "descr": {"pair": pair}} for txid, pair in self.server_open
            }
            return {"error": [], "result": {"open": open_}}
        raise AssertionError(f"unexpected endpoint {endpoint}")


class _Orders(OrdersMixin):
    def __init__(self, rest: _FakeRest, tier: KrakenAPITier = KrakenAPITier.STARTER) -> None:
        self._rest = rest
        self._endpoints: dict = {}
        self._config = KrakenConfig(api_key="k", secret_key="s", tier=tier)

    @property
    def ready(self) -> bool:
        return True


@pytest.fixture
def rest() -> _FakeRest:
    return _FakeRest()


@pytest.fixture
def orders(rest: _FakeRest) -> _Orders:
    return _Orders(rest)


async def _place(orders: _Orders, pair: str = PAIR) -> str:
    return await orders.place_order(pair, OrderType.MARKET, TradeType.BUY, Decimal("1"))


async def _fill(orders: _Orders, n: int, pair: str = PAIR) -> list[str]:
    return [await _place(orders, pair) for _ in range(n)]


def _batch(n: int) -> list[dict[str, str]]:
    return [
        {"type": "buy", "ordertype": "limit", "volume": "1", "price": f"{30000 + i}"}
        for i in range(n)
    ]


def _server(rest: _FakeRest, n: int, pair: str = PAIR) -> None:
    rest.server_open = [(f"OSRV{i:03d}-{pair}", pair) for i in range(n)]


class TestCapsPerTier:
    """AC1."""

    @pytest.mark.parametrize(
        ("tier", "cap"),
        [
            (KrakenAPITier.STARTER, 60),
            (KrakenAPITier.INTERMEDIATE, 80),
            (KrakenAPITier.PRO, 225),
        ],
    )
    def test_cap_pinned(self, tier: KrakenAPITier, cap: int) -> None:
        assert specs.KRAKEN_MAX_OPEN_ORDERS_PER_PAIR[tier] == cap

    async def test_mixin_uses_its_config_tier(self, rest: _FakeRest) -> None:
        orders = _Orders(rest, KrakenAPITier.INTERMEDIATE)
        await _fill(orders, 80)
        assert rest.n("open_orders") == 0  # 80 fit INTERMEDIATE; STARTER would have resynced

        _server(rest, 80)
        with pytest.raises(OpenOrdersLimitError):
            await _place(orders)


class TestBelowCap:
    """AC2."""

    async def test_no_resync_up_to_and_including_the_cap(
        self, orders: _Orders, rest: _FakeRest
    ) -> None:
        await _fill(orders, CAP)

        assert rest.n("open_orders") == 0
        assert rest.n("add_order") == CAP

    async def test_count_increments_on_place(self, orders: _Orders, rest: _FakeRest) -> None:
        await _fill(orders, CAP)
        _server(rest, CAP)

        with pytest.raises(OpenOrdersLimitError):
            await _place(orders)  # 61st: count 60 + 1 > cap

        assert rest.n("open_orders") == 1

    async def test_count_increments_by_n_on_batch(self, orders: _Orders, rest: _FakeRest) -> None:
        await orders.place_orders_batch(PAIR, _batch(10))
        await _fill(orders, CAP - 10)
        assert rest.n("open_orders") == 0  # exactly at the cap, no resync yet

        _server(rest, CAP)
        with pytest.raises(OpenOrdersLimitError):
            await _place(orders)  # proves the batch counted 10, not 1

    async def test_pairs_counted_independently(self, orders: _Orders, rest: _FakeRest) -> None:
        await _fill(orders, CAP)

        await _fill(orders, CAP, OTHER_PAIR)

        assert rest.n("open_orders") == 0


class TestResyncAtCap:
    """AC3 and AC4."""

    async def test_stale_count_resyncs_then_sends(self, orders: _Orders, rest: _FakeRest) -> None:
        await _fill(orders, CAP)
        _server(rest, 10)  # most orders filled elsewhere

        txid = await _place(orders)

        assert rest.n("open_orders") == 1
        assert rest.n("add_order") == CAP + 1
        assert txid.startswith("O")

    async def test_count_reset_to_resynced_value_plus_one(
        self, orders: _Orders, rest: _FakeRest
    ) -> None:
        await _fill(orders, CAP)
        _server(rest, 10)
        await _place(orders)  # resync -> 10, +1 = 11

        await _fill(orders, CAP - 11)  # 11 -> 60: no further resync
        assert rest.n("open_orders") == 1

        await _place(orders)  # 61 > cap: resync again
        assert rest.n("open_orders") == 2

    async def test_resync_counts_only_the_requested_pair(
        self, orders: _Orders, rest: _FakeRest
    ) -> None:
        await _fill(orders, CAP)
        _server(rest, CAP - 1)
        rest.server_open += [(f"OOTH{i:03d}", OTHER_PAIR) for i in range(30)]

        await _place(orders)  # 59 of PAIR open -> room for one

        assert rest.n("add_order") == CAP + 1

    async def test_full_after_resync_raises_without_sending(
        self, orders: _Orders, rest: _FakeRest
    ) -> None:
        await _fill(orders, CAP)
        _server(rest, CAP)

        with pytest.raises(OpenOrdersLimitError):
            await _place(orders)

        assert rest.n("open_orders") == 1
        assert rest.n("add_order") == CAP  # AddOrder NOT sent

    async def test_other_pair_not_blocked_by_full_pair(
        self, orders: _Orders, rest: _FakeRest
    ) -> None:
        await _fill(orders, CAP)

        await _place(orders, OTHER_PAIR)

        assert rest.n("open_orders") == 0
        assert rest.n("add_order") == CAP + 1


class TestBatchPlace:
    """AC5."""

    async def test_batch_that_would_exceed_resyncs_and_raises_when_full(
        self, orders: _Orders, rest: _FakeRest
    ) -> None:
        await _fill(orders, CAP - 2)
        _server(rest, CAP - 2)

        with pytest.raises(OpenOrdersLimitError):
            await orders.place_orders_batch(PAIR, _batch(3))  # 58 + 3 > 60

        assert rest.n("open_orders") == 1
        assert rest.n("add_order_batch") == 0

    async def test_batch_that_would_exceed_sends_when_resync_shows_room(
        self, orders: _Orders, rest: _FakeRest
    ) -> None:
        await _fill(orders, CAP - 2)
        _server(rest, 50)

        txids = await orders.place_orders_batch(PAIR, _batch(3))

        assert len(txids) == 3
        assert rest.n("open_orders") == 1
        assert rest.n("add_order_batch") == 1

    async def test_batch_reaching_exactly_the_cap_does_not_resync(
        self, orders: _Orders, rest: _FakeRest
    ) -> None:
        await _fill(orders, CAP - 2)

        await orders.place_orders_batch(PAIR, _batch(2))

        assert rest.n("open_orders") == 0
        assert rest.n("add_order_batch") == 1

    async def test_count_after_resynced_batch_is_resynced_plus_n(
        self, orders: _Orders, rest: _FakeRest
    ) -> None:
        await _fill(orders, CAP - 2)
        _server(rest, 50)
        await orders.place_orders_batch(PAIR, _batch(3))  # 50 + 3 = 53

        await _fill(orders, CAP - 53)  # 53 -> 60: no further resync
        assert rest.n("open_orders") == 1

        await _place(orders)
        assert rest.n("open_orders") == 2


class TestCancelDecrements:
    """AC6."""

    async def test_cancel_order_frees_one_slot(self, orders: _Orders, rest: _FakeRest) -> None:
        txids = await _fill(orders, CAP)

        assert await orders.cancel_order(PAIR, txids[0]) is True
        await _place(orders)  # 59 + 1 = 60: fits without resync

        assert rest.n("open_orders") == 0

        await _place(orders)  # 61: resync
        assert rest.n("open_orders") == 1

    async def test_cancel_on_other_pair_does_not_free_slot(
        self, orders: _Orders, rest: _FakeRest
    ) -> None:
        await _fill(orders, CAP)
        other = await _place(orders, OTHER_PAIR)
        await orders.cancel_order(OTHER_PAIR, other)
        _server(rest, CAP)

        with pytest.raises(OpenOrdersLimitError):
            await _place(orders)

    async def test_count_never_goes_negative(self, orders: _Orders, rest: _FakeRest) -> None:
        await orders.cancel_order(PAIR, "OUNKNOWN-AAAAA-AAAAAA")  # nothing counted
        await _fill(orders, CAP)
        assert rest.n("open_orders") == 0

        _server(rest, CAP)
        with pytest.raises(OpenOrdersLimitError):
            await _place(orders)  # a negative count would have let this through

    async def test_cancel_orders_batch_decrements_per_pair(
        self, orders: _Orders, rest: _FakeRest
    ) -> None:
        btc = await _fill(orders, CAP)
        eth = await _fill(orders, CAP, OTHER_PAIR)

        count = await orders.cancel_orders_batch(
            [(PAIR, btc[0]), (PAIR, btc[1]), (PAIR, btc[2]), (OTHER_PAIR, eth[0])]
        )

        assert count == 4
        await _fill(orders, 3)  # BTC 57 -> 60: no resync
        await _fill(orders, 1, OTHER_PAIR)  # ETH 59 -> 60: no resync
        assert rest.n("open_orders") == 0

        await _place(orders)  # BTC 61: resync
        assert rest.n("open_orders") == 1

    async def test_cancel_orders_batch_leaves_unaffected_pair_at_cap(
        self, orders: _Orders, rest: _FakeRest
    ) -> None:
        await _fill(orders, CAP, OTHER_PAIR)
        btc = await _fill(orders, 5)
        await orders.cancel_orders_batch([(PAIR, t) for t in btc])
        _server(rest, CAP, OTHER_PAIR)

        with pytest.raises(OpenOrdersLimitError):
            await _place(orders, OTHER_PAIR)


class TestServerSideRejection:
    """AC7."""

    def test_error_code_maps_to_open_orders_limit_error(self) -> None:
        assert ERROR_CODE_MAPPING[LIMIT_ERROR] is OpenOrdersLimitError

    def test_is_an_order_rejection(self) -> None:
        assert issubclass(OpenOrdersLimitError, OrderRejectedError)
        assert errors.OpenOrdersLimitError is OpenOrdersLimitError

    def test_raise_on_kraken_error_raises_it(self) -> None:
        with pytest.raises(OpenOrdersLimitError) as info:
            raise_on_kraken_error([LIMIT_ERROR])
        assert isinstance(info.value, OrderRejectedError)

    async def test_place_order_surfaces_it(self, orders: _Orders, rest: _FakeRest) -> None:
        rest.reject_add = [LIMIT_ERROR]

        with pytest.raises(OpenOrdersLimitError):
            await _place(orders)

    async def test_rejection_does_not_increment_count(
        self, orders: _Orders, rest: _FakeRest
    ) -> None:
        await _fill(orders, CAP - 1)
        rest.reject_add = [LIMIT_ERROR]
        with pytest.raises(OpenOrdersLimitError):
            await _place(orders)
        rest.reject_add = []

        await _place(orders)  # 59 + 1 = 60: no resync iff the rejection was not counted

        assert rest.n("open_orders") == 0

    async def test_batch_rejection_does_not_increment_count(
        self, orders: _Orders, rest: _FakeRest
    ) -> None:
        await _fill(orders, CAP - 2)
        rest.reject_add = [LIMIT_ERROR]
        with pytest.raises(OpenOrdersLimitError):
            await orders.place_orders_batch(PAIR, _batch(2))
        rest.reject_add = []

        await orders.place_orders_batch(PAIR, _batch(2))  # 58 + 2 = 60: no resync

        assert rest.n("open_orders") == 0


class TestEditAndAmendKeepCount:
    """AC8."""

    async def test_edit_does_not_increment(self, orders: _Orders, rest: _FakeRest) -> None:
        txids = await _fill(orders, CAP - 1)
        await orders.edit_order(PAIR, txids[0], price=Decimal("1"))

        await _place(orders)  # 59 + 1 = 60: no resync

        assert rest.n("open_orders") == 0

    async def test_edit_does_not_decrement(self, orders: _Orders, rest: _FakeRest) -> None:
        txids = await _fill(orders, CAP)
        await orders.edit_order(PAIR, txids[0], price=Decimal("1"))
        _server(rest, CAP)

        with pytest.raises(OpenOrdersLimitError):
            await _place(orders)

        assert rest.n("open_orders") == 1

    async def test_edited_order_can_be_cancelled_and_frees_a_slot(
        self, orders: _Orders, rest: _FakeRest
    ) -> None:
        txids = await _fill(orders, CAP)
        new_txid = await orders.edit_order(PAIR, txids[0], price=Decimal("1"))
        await orders.cancel_order(PAIR, new_txid)

        await _place(orders)  # 59 + 1: no resync

        assert rest.n("open_orders") == 0
