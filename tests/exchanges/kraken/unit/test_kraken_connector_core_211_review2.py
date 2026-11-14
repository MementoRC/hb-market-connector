"""Second code-review regressions for the Kraken connector core (issue #211), RED step.

Public behaviour only (bus events, ``fetch_order``, the requests the faked REST
transport receives). Fixtures and helpers come from ``test_kraken_connector_core_211``
and ``..._review``; ``rest`` is overridden to also signal every request.

Skipped: nothing. S9 is observable through public behaviour by driving the core's
``time`` source (see ``TestS9PendingBufferTtl``).
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from data_type_primitives.in_flight_order import OrderState

from market_connector.exchanges.kraken.schemas.ws import OwnTradesEvent
from tests.exchanges.kraken.unit import test_kraken_connector_core_211 as base
from tests.exchanges.kraken.unit import test_kraken_connector_core_211_review as review

BUY_COMPLETED, BUY_CREATED = base.BUY_COMPLETED, base.BUY_CREATED
SELL_COMPLETED, SELL_CREATED = base.SELL_COMPLETED, base.SELL_CREATED
CANCELLED, FILLED = base.CANCELLED, base.FILLED
_AMOUNT, _PAIR, _PRICE, _TXID = base._AMOUNT, base._PAIR, base._PRICE, base._TXID  # noqa: SLF001
_INVALID_ORDER_ID = base._INVALID_ORDER_ID  # noqa: SLF001
_order_info, _trade_info = base._order_info, base._trade_info  # noqa: SLF001
_place, _ws_trades = base._place, base._ws_trades  # noqa: SLF001
OrderType, Recorder = base.OrderType, base.Recorder

bare_connector, bus, connector = base.bare_connector, base.bus, base.connector
gateway, seen, make_connector = base.gateway, base.seen, base.make_connector


class SignalRest(review.ReviewRest):
    """ReviewRest that signals every request so tests can wait without sleeping."""

    def __init__(self) -> None:
        super().__init__()
        self._request_seen = asyncio.Event()

    async def request(self, endpoint_name: str, *args: Any, **kwargs: Any) -> Any:
        try:
            return await super().request(endpoint_name, *args, **kwargs)
        finally:
            self._request_seen.set()

    async def wait_calls(self, endpoint: str, count: int, timeout: float = 5.0) -> None:
        async def _until() -> None:
            while len(self.bodies(endpoint)) < count:
                self._request_seen.clear()
                await self._request_seen.wait()

        await asyncio.wait_for(_until(), timeout)


@pytest.fixture
def rest() -> SignalRest:
    return SignalRest()


def _ids(seen: Recorder) -> list[int]:
    return [eid for eid, _ in seen.events]


def _frame(*trades: tuple[str, str, str], seq: int = 1) -> OwnTradesEvent:
    """An ownTrades frame with several ``(trade_id, txid, vol)`` trades (price 27000)."""
    entries = [
        {
            trade_id: {
                "ordertxid": txid,
                "pair": "XBT/USD",
                "time": "1700000000.5",
                "type": "buy",
                "ordertype": "limit",
                "price": "27000.0",
                "cost": str(Decimal(vol) * 27000),
                "fee": "0",
                "vol": vol,
                "margin": "0",
            }
        }
        for trade_id, txid, vol in trades
    ]
    return OwnTradesEvent.from_raw_frame([entries, "ownTrades", {"sequence": seq}])


def _raise(_: Any) -> None:
    raise RuntimeError("subscriber boom")


# ---------------------------------------------------------------------------
# S2: cancel ack syncs unreported fills first
# ---------------------------------------------------------------------------


class TestS2CancelSyncsFills:
    async def test_unreported_partial_fill_is_booked_before_106(
        self, connector: Any, rest: SignalRest, seen: Recorder
    ) -> None:
        client_id = await _place(connector, seen)
        rest.trades["T1"] = _trade_info(
            vol="0.2", price="27000.0", cost="5400.0", fee="8.64", time=1700000000.5
        )
        rest.orders[_TXID] = _order_info(
            status="open", vol_exec="0.2", cost="5400.0", price="27000.0", trades=("T1",)
        )
        connector.cancel(_PAIR, client_id)
        await seen.wait(CANCELLED)

        assert _ids(seen) == [BUY_CREATED, FILLED, CANCELLED]
        assert seen.of(FILLED)[0].exchange_trade_id == "T1"
        assert connector.fetch_order(client_id).executed_amount_base == Decimal("0.2")


# ---------------------------------------------------------------------------
# S3: a raising subscriber must not break replay / remembered cancel / batches
# ---------------------------------------------------------------------------


class TestS3RaisingSubscribers:
    async def test_raising_created_subscriber_keeps_early_fill_replay(
        self, connector: Any, rest: SignalRest, seen: Recorder, bus: Any
    ) -> None:
        bus.subscribe(BUY_CREATED, _raise)
        rest.add_order_gate.clear()
        client_id = connector.buy(_PAIR, _AMOUNT, OrderType.LIMIT, _PRICE)
        await rest.add_order_started.wait()
        connector.on_own_trades(
            _ws_trades(
                "T1", vol="0.2", price="27000.0", cost="5400.0", fee="0", time="1700000000.5"
            )
        )
        rest.add_order_gate.set()
        await seen.wait(FILLED)

        assert _ids(seen) == [BUY_CREATED, FILLED]
        assert connector.fetch_order(client_id).executed_amount_base == Decimal("0.2")

    async def test_raising_created_subscriber_keeps_remembered_cancel(
        self, connector: Any, rest: SignalRest, seen: Recorder, bus: Any
    ) -> None:
        bus.subscribe(BUY_CREATED, _raise)
        rest.add_order_gate.clear()
        client_id = connector.buy(_PAIR, _AMOUNT, OrderType.LIMIT, _PRICE)
        await rest.add_order_started.wait()
        connector.cancel(_PAIR, client_id)
        rest.add_order_gate.set()
        await seen.wait(CANCELLED)

        assert rest.bodies("cancel_order") == [{"txid": _TXID}]
        assert _ids(seen) == [BUY_CREATED, CANCELLED]

    async def test_raising_fill_subscriber_does_not_skip_rest_of_batch(
        self, connector: Any, seen: Recorder, bus: Any
    ) -> None:
        client_id = await _place(connector, seen)
        bus.subscribe(FILLED, _raise)
        connector.on_own_trades(_frame(("T1", _TXID, "0.2"), ("T2", _TXID, "0.1")))  # no raise

        assert [f.exchange_trade_id for f in seen.of(FILLED)] == ["T1", "T2"]
        assert connector.fetch_order(client_id).executed_amount_base == Decimal("0.3")


# ---------------------------------------------------------------------------
# S4: closed but under-filled -> 106, not 102/103
# ---------------------------------------------------------------------------


class TestS4ClosedPartial:
    async def test_closed_with_less_than_volume_is_cancelled(
        self, connector: Any, rest: SignalRest, seen: Recorder
    ) -> None:
        client_id = await _place(connector, seen)
        rest.trades["T1"] = _trade_info(
            vol="0.2", price="27000.0", cost="5400.0", fee="8.64", time=1700000000.5
        )
        rest.orders[_TXID] = _order_info(
            status="closed", vol_exec="0.2", cost="5400.0", price="27000.0", trades=("T1",)
        )
        await connector.update_order_status(client_id)

        assert _ids(seen) == [BUY_CREATED, FILLED, CANCELLED]
        assert seen.of(BUY_COMPLETED) == []
        assert connector.fetch_order(client_id).executed_amount_base == Decimal("0.2")

    async def test_closed_with_full_volume_still_completes(
        self, connector: Any, rest: SignalRest, seen: Recorder
    ) -> None:
        client_id = await _place(connector, seen)
        rest.trades["T1"] = _trade_info(
            vol="0.5", price="27000.0", cost="13500.0", fee="21.6", time=1700000000.5
        )
        rest.orders[_TXID] = _order_info(
            status="closed", vol_exec="0.5", cost="13500.0", price="27000.0", trades=("T1",)
        )
        await connector.update_order_status(client_id)
        assert _ids(seen) == [BUY_CREATED, FILLED, BUY_COMPLETED]
        assert connector.fetch_order(client_id).is_open is False


# ---------------------------------------------------------------------------
# S5: status-derived fill, then the real WS trades arrive
# ---------------------------------------------------------------------------


class TestS5StatusFillThenRealTrades:
    async def test_real_trades_after_status_fill_are_not_double_booked(
        self, connector: Any, rest: SignalRest, seen: Recorder
    ) -> None:
        client_id = await _place(connector, seen, "sell", OrderType.STOP_LOSS, Decimal("26950.5"))
        rest.trades_error = _INVALID_ORDER_ID
        rest.orders[_TXID] = _order_info(
            status="closed",
            vol_exec="0.5",
            cost="13475.0",
            price="26950.0",
            ordertype="stop-loss",
            side="sell",
            trades=("T9",),
        )
        await connector.update_order_status(client_id)
        assert len(seen.of(FILLED)) == 1  # status-derived
        assert len(seen.of(SELL_COMPLETED)) == 1

        connector.on_own_trades(_frame(("R1", _TXID, "0.3"), ("R2", _TXID, "0.2")))

        assert len(seen.of(FILLED)) == 1  # no additional 107
        assert len(seen.of(SELL_COMPLETED)) == 1
        assert connector.fetch_order(client_id).executed_amount_base <= _AMOUNT


# ---------------------------------------------------------------------------
# S7: batched QueryOrders polling
# ---------------------------------------------------------------------------


class TestS7BatchedPolling:
    async def test_three_open_orders_are_polled_in_one_request(
        self, make_connector: Any, rest: SignalRest, seen: Recorder
    ) -> None:
        core = await make_connector(0.01)
        txids = []
        for n in range(1, 4):
            core.buy(_PAIR, _AMOUNT, OrderType.LIMIT, _PRICE)
            txids.append(f"OTXID-{n}")
            rest.orders[f"OTXID-{n}"] = _order_info(status="open")
        await seen.wait(BUY_CREATED, 3)
        await core.start()
        try:
            await rest.wait_calls("query_orders", 1)
        finally:
            await core.stop()

        first = rest.bodies("query_orders")[0]
        assert sorted(first["txid"].split(",")) == sorted(txids)  # all 3 in ONE request

    async def test_batches_never_exceed_50_ids(
        self, make_connector: Any, rest: SignalRest, seen: Recorder
    ) -> None:
        core = await make_connector(0.01)
        pairs = ["BTC-USD", "ETH-USD", "SOL-USD"]
        expected: set[str] = set()
        for n in range(1, 121):
            core.buy(pairs[n % 3], Decimal("0.5"), OrderType.LIMIT, Decimal("100"))
            expected.add(f"OTXID-{n}")
            rest.orders[f"OTXID-{n}"] = _order_info(status="open")
        await seen.wait(BUY_CREATED, 120, timeout=15.0)
        await core.start()
        try:
            await rest.wait_calls("query_orders", 3)
        finally:
            await core.stop()

        first_cycle = rest.bodies("query_orders")[:3]
        sizes = [len(b["txid"].split(",")) for b in first_cycle]
        assert max(sizes) <= 50
        assert sum(sizes) == 120
        assert {t for b in first_cycle for t in b["txid"].split(",")} == expected


# ---------------------------------------------------------------------------
# S9: the pending-trade buffer expires entries even when a key is re-added
# ---------------------------------------------------------------------------


class TestS9PendingBufferTtl:
    async def test_expired_early_fill_is_not_replayed(
        self,
        connector: Any,
        rest: SignalRest,
        seen: Recorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import market_connector.exchanges.kraken.hb_compat.kraken_connector_core as core_mod  # noqa: PLC0415

        now = [1000.0]
        monkeypatch.setattr(
            core_mod, "time", SimpleNamespace(time=time.time, monotonic=lambda: now[0])
        )
        rest.add_order_gate.clear()
        connector.buy(_PAIR, _AMOUNT, OrderType.LIMIT, _PRICE)  # becomes OTXID-1
        connector.buy(_PAIR, _AMOUNT, OrderType.LIMIT, _PRICE)  # becomes OTXID-2
        await rest.add_order_started.wait()

        connector.on_own_trades(_frame(("A", "OTXID-2", "0.1")))  # t=0
        now[0] += 30
        connector.on_own_trades(_frame(("B", "OTXID-1", "0.1")))  # t=30
        now[0] += 20
        connector.on_own_trades(_frame(("A", "OTXID-2", "0.1")))  # A re-added at t=50
        now[0] += 45  # t=95: B is 65s old (expired), re-added A is 45s old (live)
        rest.add_order_gate.set()
        await seen.wait(BUY_CREATED, 2)

        assert [f.exchange_trade_id for f in seen.of(FILLED)] == ["A"]  # B is gone


# ---------------------------------------------------------------------------
# S10 / S11
# ---------------------------------------------------------------------------


class TestS10WsPartialState:
    async def test_ws_only_partial_fill_marks_partially_filled(
        self, connector: Any, seen: Recorder
    ) -> None:
        client_id = await _place(connector, seen)
        connector.on_own_trades(_frame(("T1", _TXID, "0.2")))
        assert connector.fetch_order(client_id).current_state is OrderState.PARTIALLY_FILLED
        assert connector.fetch_order(client_id).is_open is True


class TestS11DoubleCancel:
    async def test_two_cancels_send_one_request_and_one_106(
        self, connector: Any, rest: SignalRest, seen: Recorder
    ) -> None:
        client_id = await _place(connector, seen)
        connector.cancel(_PAIR, client_id)
        connector.cancel(_PAIR, client_id)
        await seen.wait(CANCELLED)
        await connector.stop()  # drain background tasks

        assert rest.bodies("cancel_order") == [{"txid": _TXID}]
        assert len(seen.of(CANCELLED)) == 1
