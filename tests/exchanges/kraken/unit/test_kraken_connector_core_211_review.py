"""Code-review regressions for the Kraken connector core (issue #211), RED step.

Targets public behaviour only: events on the bus, ``fetch_order``, ``in_flight_orders``
and the requests the (faked) REST transport receives. Races are driven by holding a
faked REST reply on an ``asyncio.Event``.

Fixtures and helpers are reused from ``test_kraken_connector_core_211`` (``rest`` is
overridden here with a transport that can hold replies and raise transport errors).

Pinned rules (see the cases below):
* R9: AssetPairs entries without a ``wsname`` are skipped; for two entries mapping to the
  same hummingbot pair the FIRST one (AssetPairs order) wins.
* R2b: for one order the events are ``Created < Filled* < terminal``; nothing is emitted
  for an order after its terminal event, except a genuine late WS fill (R2a).
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
from decimal import Decimal
from typing import Any

import httpx
import pytest

from tests.exchanges.kraken.unit import test_kraken_connector_core_211 as base

# Constants and helpers reused from the main file.
BUY_COMPLETED, BUY_CREATED = base.BUY_COMPLETED, base.BUY_CREATED
SELL_COMPLETED, SELL_CREATED = base.SELL_COMPLETED, base.SELL_CREATED
CANCELLED, FAILURE, FILLED = base.CANCELLED, base.FAILURE, base.FILLED
_AMOUNT, _PAIR, _PRICE, _TXID = base._AMOUNT, base._PAIR, base._PRICE, base._TXID  # noqa: SLF001
_INVALID_ORDER_ID = base._INVALID_ORDER_ID  # noqa: SLF001
_order_info, _trade_info = base._order_info, base._trade_info  # noqa: SLF001
_place, _ws_trades = base._place, base._ws_trades  # noqa: SLF001
FakeKrakenRest, OrderType, Recorder = base.FakeKrakenRest, base.OrderType, base.Recorder

# Fixtures reused as-is (pytest discovers them by module attribute); ``rest`` is overridden.
bare_connector, bus, connector = base.bare_connector, base.bus, base.connector
gateway, seen = base.gateway, base.seen

_GRACE = 20.0  # upper bound for a stop() that has to cancel a stuck task


class ReviewRest(FakeKrakenRest):
    """FakeKrakenRest whose AddOrder / QueryTrades replies can be held or made to fail."""

    def __init__(self) -> None:
        super().__init__()
        self.add_order_gate = asyncio.Event()
        self.add_order_gate.set()
        self.add_order_started = asyncio.Event()
        self.query_trades_gate = asyncio.Event()
        self.query_trades_gate.set()
        self.query_trades_started = asyncio.Event()
        self.query_trades_exc: Exception | None = None

    async def request(self, endpoint_name: str, *args: Any, **kwargs: Any) -> Any:
        if endpoint_name == "add_order":
            self.add_order_started.set()
            await self.add_order_gate.wait()
        elif endpoint_name == "query_trades":
            self.query_trades_started.set()
            await self.query_trades_gate.wait()
            if self.query_trades_exc is not None:
                raise self.query_trades_exc
        return await super().request(endpoint_name, *args, **kwargs)


@pytest.fixture
def rest() -> ReviewRest:
    return ReviewRest()


def _ids(seen: Recorder) -> list[int]:
    return [eid for eid, _ in seen.events]


# ---------------------------------------------------------------------------
# R1: a WS fill that arrives before AddOrder returns is buffered
# ---------------------------------------------------------------------------


class TestR1FillBeforeTxid:
    async def test_early_ws_fill_is_booked_after_created(
        self, connector: Any, rest: ReviewRest, seen: Recorder
    ) -> None:
        rest.add_order_gate.clear()
        client_id = connector.buy(_PAIR, _AMOUNT, OrderType.LIMIT, _PRICE)  # no start()
        await rest.add_order_started.wait()
        connector.on_own_trades(
            _ws_trades(
                "T1", vol="0.5", price="27000.0", cost="13500.0", fee="21.6", time="1700000000.5"
            )
        )
        assert seen.events == []  # txid unknown yet: nothing can be attributed
        rest.add_order_gate.set()
        await seen.wait(BUY_COMPLETED)

        assert _ids(seen) == [BUY_CREATED, FILLED, BUY_COMPLETED]
        (fill,) = seen.of(FILLED)
        assert fill.order_id == client_id
        assert fill.exchange_trade_id == "T1"
        assert isinstance(fill.timestamp, float)
        assert connector.fetch_order(client_id).is_open is False


# ---------------------------------------------------------------------------
# R2: fills racing with cancellation
# ---------------------------------------------------------------------------


class TestR2LateFills:
    async def test_late_ws_fill_after_cancel_is_booked(
        self, connector: Any, seen: Recorder
    ) -> None:
        client_id = await _place(connector, seen)
        connector.cancel(_PAIR, client_id)
        await seen.wait(CANCELLED)

        connector.on_own_trades(
            _ws_trades(
                "T1", vol="0.2", price="27000.0", cost="5400.0", fee="8.64", time="1700000000.5"
            )
        )

        (fill,) = seen.of(FILLED)
        assert fill.order_id == client_id
        assert fill.amount == Decimal("0.2")
        assert connector.fetch_order(client_id).executed_amount_base == Decimal("0.2")
        assert len(seen.of(CANCELLED)) == 1  # no second 106
        assert seen.of(BUY_COMPLETED) == []

    async def test_cancel_during_query_trades_books_the_trade_as_a_late_fill(
        self, connector: Any, rest: ReviewRest, seen: Recorder
    ) -> None:
        # Order rule: Created < Filled* < terminal, then optionally late Filled* only.
        client_id = await _place(connector, seen)
        rest.trades["T1"] = _trade_info(
            vol="0.2", price="27000.0", cost="5400.0", fee="8.64", time=1700000000.5
        )
        rest.orders[_TXID] = _order_info(
            status="open", vol_exec="0.2", cost="5400.0", price="27000.0", trades=("T1",)
        )
        rest.query_trades_gate.clear()
        poll = asyncio.create_task(connector.update_order_status(client_id))
        await rest.query_trades_started.wait()

        connector.cancel(_PAIR, client_id)
        await seen.wait(CANCELLED)
        rest.query_trades_gate.set()
        await poll  # QueryTrades now returns a trade executed before the cancel

        # The same execution reported again by the WS feed must not double count.
        connector.on_own_trades(
            _ws_trades(
                "T1", vol="0.2", price="27000.0", cost="5400.0", fee="8.64", time="1700000000.5"
            )
        )

        assert _ids(seen) == [BUY_CREATED, CANCELLED, FILLED]  # exactly one late 107
        (fill,) = seen.of(FILLED)
        assert fill.exchange_trade_id == "T1"
        assert fill.amount == Decimal("0.2")
        assert len(seen.of(CANCELLED)) == 1  # no second 106
        assert seen.of(BUY_COMPLETED) == []
        assert connector.fetch_order(client_id).executed_amount_base == Decimal("0.2")
        assert connector.fetch_order(client_id).is_open is False


# ---------------------------------------------------------------------------
# R3: stop() with background create/cancel tasks in flight
# ---------------------------------------------------------------------------


@pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning")
class TestR3StopWithTasksInFlight:
    async def test_stop_waits_for_a_create_that_finishes_within_grace(
        self, connector: Any, rest: ReviewRest, seen: Recorder
    ) -> None:
        before = asyncio.all_tasks()
        await connector.start()
        rest.add_order_gate.clear()
        client_id = connector.buy(_PAIR, _AMOUNT, OrderType.LIMIT, _PRICE)
        await rest.add_order_started.wait()
        asyncio.get_running_loop().call_later(0.05, rest.add_order_gate.set)

        await asyncio.wait_for(connector.stop(), _GRACE)

        assert asyncio.all_tasks() <= before
        assert seen.of(BUY_CREATED)  # the create was allowed to finish
        assert connector.fetch_order(client_id).exchange_order_id == _TXID
        gc.collect()

    async def test_stop_cancels_a_stuck_create_and_keeps_the_order_tracked(
        self, connector: Any, rest: ReviewRest, seen: Recorder
    ) -> None:
        before = asyncio.all_tasks()
        await connector.start()
        rest.add_order_gate.clear()  # AddOrder never returns
        client_id = connector.buy(_PAIR, _AMOUNT, OrderType.LIMIT, _PRICE)
        await rest.add_order_started.wait()

        await asyncio.wait_for(connector.stop(), _GRACE)

        assert asyncio.all_tasks() <= before  # no task outlives stop()
        order = connector.fetch_order(client_id)
        assert order is not None  # not silently dropped
        assert order.is_open is True
        assert client_id in connector.in_flight_orders
        assert seen.of(FAILURE) == []  # outcome unknown, not a failure
        gc.collect()


# ---------------------------------------------------------------------------
# R4: transient QueryTrades errors must not synthesize fills
# ---------------------------------------------------------------------------


class TestR4TransientQueryTradesError:
    async def test_transient_error_then_real_trades_are_booked(
        self, connector: Any, rest: ReviewRest, seen: Recorder
    ) -> None:
        client_id = await _place(connector, seen)
        rest.trades["T1"] = _trade_info(
            vol="0.2", price="27000.0", cost="5400.0", fee="8.64", time=1700000000.5
        )
        rest.trades["T2"] = _trade_info(
            vol="0.3", price="27100.0", cost="8130.0", fee="13.0", time=1700000001.5
        )
        rest.orders[_TXID] = _order_info(
            status="closed",
            vol_exec="0.5",
            cost="13530.0",
            price="27060.0",
            trades=("T1", "T2"),
        )
        rest.query_trades_exc = httpx.ConnectError("connection reset")

        with contextlib.suppress(Exception):  # raising or swallowing are both acceptable
            await connector.update_order_status(client_id)

        assert _ids(seen) == [BUY_CREATED]  # no synthesized fill, no completion
        assert connector.fetch_order(client_id).is_open is True
        assert client_id in connector.in_flight_orders

        rest.query_trades_exc = None
        await connector.update_order_status(client_id)

        fills = seen.of(FILLED)
        assert [(f.exchange_trade_id, f.price) for f in fills] == [
            ("T1", Decimal("27000.0")),
            ("T2", Decimal("27100.0")),
        ]
        (done,) = seen.of(BUY_COMPLETED)
        assert done.quote_asset_amount == Decimal("13530.0")


# ---------------------------------------------------------------------------
# R5: canceled/expired triggered order with executed volume
# ---------------------------------------------------------------------------


class TestR5CanceledWithExecutedVolume:
    @pytest.mark.parametrize("status", ["canceled", "expired"])
    async def test_executed_volume_is_booked_before_cancelled(
        self, connector: Any, rest: ReviewRest, seen: Recorder, status: str
    ) -> None:
        client_id = await _place(connector, seen, "sell", OrderType.STOP_LOSS, Decimal("26950.5"))
        rest.trades_error = _INVALID_ORDER_ID
        rest.orders[_TXID] = _order_info(
            status=status,
            vol_exec="0.2",
            cost="5390.0",
            price="26950.0",
            ordertype="stop-loss",
            side="sell",
            trades=("T9",),
        )
        await connector.update_order_status(client_id)

        assert _ids(seen) == [SELL_CREATED, FILLED, CANCELLED]
        (fill,) = seen.of(FILLED)
        assert fill.amount == Decimal("0.2")
        assert isinstance(fill.timestamp, float)
        assert connector.fetch_order(client_id).executed_amount_base == Decimal("0.2")
        assert seen.of(SELL_COMPLETED) == []


# ---------------------------------------------------------------------------
# R6: cancel() while the order is still PENDING_CREATE
# ---------------------------------------------------------------------------


class TestR6CancelWhilePendingCreate:
    async def test_create_rejected_after_cancel_fails_cleanly(
        self, connector: Any, rest: ReviewRest, seen: Recorder
    ) -> None:
        before = asyncio.all_tasks()
        rest.add_order_gate.clear()
        rest.add_order_errors = ["EOrder:Insufficient funds"]
        client_id = connector.buy(_PAIR, _AMOUNT, OrderType.LIMIT, _PRICE)
        await rest.add_order_started.wait()
        connector.cancel(_PAIR, client_id)
        rest.add_order_gate.set()

        await seen.wait(FAILURE)
        await asyncio.wait_for(connector.stop(), 1.0)  # nothing may hang on the order id

        assert asyncio.all_tasks() <= before  # the cancel task did not linger
        assert rest.bodies("cancel_order") == []
        assert seen.of(CANCELLED) == []
        assert _ids(seen) == [FAILURE]
        assert client_id not in connector.in_flight_orders

    async def test_cancel_is_not_lost_when_create_is_slow(
        self,
        connector: Any,
        rest: ReviewRest,
        seen: Recorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import data_type_primitives.in_flight_order as dtp_order  # noqa: PLC0415

        monkeypatch.setattr(dtp_order, "GET_EX_ORDER_ID_TIMEOUT", 0.05)  # shrink the id wait
        rest.add_order_gate.clear()
        client_id = connector.buy(_PAIR, _AMOUNT, OrderType.LIMIT, _PRICE)
        await rest.add_order_started.wait()
        connector.cancel(_PAIR, client_id)
        asyncio.get_running_loop().call_later(0.3, rest.add_order_gate.set)  # >> the id wait

        await seen.wait(CANCELLED, timeout=5.0)

        assert rest.bodies("cancel_order") == [{"txid": _TXID}]
        assert _ids(seen) == [BUY_CREATED, CANCELLED]
        assert seen.of(CANCELLED)[0].order_id == client_id
        assert connector.fetch_order(client_id).is_open is False


# ---------------------------------------------------------------------------
# R7: completion is decided in Decimal
# ---------------------------------------------------------------------------


class TestR7DecimalCompletion:
    async def _place_small(self, connector: Any, seen: Recorder) -> str:
        client_id = connector.buy(_PAIR, Decimal("0.1"), OrderType.LIMIT, _PRICE)
        await seen.wait(BUY_CREATED)
        return client_id

    async def test_a_hair_short_of_the_amount_is_not_completed(
        self, connector: Any, seen: Recorder
    ) -> None:
        client_id = await self._place_small(connector, seen)
        for trade_id, vol in (("T1", "0.05"), ("T2", "0.049999999999")):  # 0.1 - 1e-12
            connector.on_own_trades(
                _ws_trades(trade_id, vol=vol, price="27000", cost="1", fee="0", time="1700000000.5")
            )
        assert len(seen.of(FILLED)) == 2
        assert seen.of(BUY_COMPLETED) == []
        assert connector.fetch_order(client_id).is_open is True

    async def test_overfill_completes_exactly_once_without_raising(
        self, connector: Any, seen: Recorder
    ) -> None:
        client_id = await self._place_small(connector, seen)
        for seq, (trade_id, vol) in enumerate((("T1", "0.06"), ("T2", "0.06"), ("T3", "0.01"))):
            connector.on_own_trades(
                _ws_trades(
                    trade_id,
                    vol=vol,
                    price="27000",
                    cost="1",
                    fee="0",
                    time="1700000000.5",
                    seq=seq,
                )
            )
        assert len(seen.of(BUY_COMPLETED)) == 1
        assert seen.of(BUY_COMPLETED)[0].order_id == client_id
        assert connector.fetch_order(client_id).is_open is False


# ---------------------------------------------------------------------------
# R8: quantization before AddOrder; NaN limit price
# ---------------------------------------------------------------------------


class TestR8Quantization:
    async def test_amount_and_price_are_quantized_before_add_order(
        self, connector: Any, rest: ReviewRest, seen: Recorder
    ) -> None:
        connector.buy(_PAIR, Decimal("0.123456789"), OrderType.LIMIT, Decimal("50000.14"))
        await seen.wait(BUY_CREATED)
        (body,) = rest.bodies("add_order")
        assert body["volume"] == "0.12345678"  # lot_decimals=8, rounded DOWN
        assert body["price"] == "50000.1"  # pair_decimals=1

    async def test_limit_order_with_nan_price_fails_without_a_request(
        self, connector: Any, rest: ReviewRest, seen: Recorder
    ) -> None:
        client_id = connector.buy(_PAIR, _AMOUNT, OrderType.LIMIT)  # default NaN price
        await seen.wait(FAILURE)
        assert rest.bodies("add_order") == []
        assert _ids(seen) == [FAILURE]
        assert seen.of(FAILURE)[0].order_id == client_id
        assert client_id not in connector.in_flight_orders


# ---------------------------------------------------------------------------
# R9: deterministic AssetPairs mapping
# ---------------------------------------------------------------------------


class TestR9DuplicatePairs:
    async def test_no_wsname_skipped_and_first_duplicate_wins(
        self, bare_connector: Any, rest: ReviewRest, seen: Recorder
    ) -> None:
        base = rest.asset_pairs["XXBTZUSD"]
        rest.asset_pairs = {
            "XBTUSD.d": {**base, "altname": "XBTUSD.d", "wsname": "", "ordermin": "0.5"},
            "XXBTZUSD": base,
            "XBTUSD_DUP": {**base, "altname": "XBTUSD2", "ordermin": "9"},
        }
        await bare_connector.update_trading_rules()

        assert bare_connector.trading_rules["BTC-USD"].min_order_size == Decimal("0.0001")
        bare_connector.buy(_PAIR, _AMOUNT, OrderType.LIMIT, _PRICE)
        await seen.wait(BUY_CREATED)
        assert rest.bodies("add_order")[0]["pair"] == "XBTUSD"

    async def test_mapping_is_stable_across_reloads(
        self, bare_connector: Any, rest: ReviewRest
    ) -> None:
        base = rest.asset_pairs["XXBTZUSD"]
        rest.asset_pairs = {"A": base, "B": {**base, "altname": "XBTUSD2"}}
        await bare_connector.update_trading_rules()
        first = dict(bare_connector.trading_rules)
        await bare_connector.update_trading_rules()
        assert bare_connector.trading_rules == first


# ---------------------------------------------------------------------------
# R10: NaN amount
# ---------------------------------------------------------------------------


class TestR10NanAmount:
    async def test_quantize_nan_amount_is_zero(self, connector: Any) -> None:
        assert connector.quantize_order_amount(_PAIR, Decimal("NaN")) == Decimal(0)
