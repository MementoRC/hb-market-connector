"""REST late fills (issue #286), RED step.

Target behaviour on ``KrakenConnectorCore`` / the gateway REST path:

* QueryOrders requests carry ``trades=true`` (Kraken returns ``trades`` only on request).
* QueryTrades is chunked to at most 20 txids per call.
* An order that went terminal with ``executed_amount_base < vol_exec`` enters a reconcile
  set (``core._reconciling``: dict cid -> started_at) and keeps being polled until booked
  volume reaches ``vol_exec`` (no second terminal event) or ``finished_order_grace_s``
  elapses (WARNING with the txid + ``late_trade_drops`` += 1). The set is bounded by
  ``max_finished_orders_hard``.

The fake is STRICT: it mirrors Kraken (no ``trades`` field without the flag; >20 ids in
QueryTrades is an error).
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest

from tests.exchanges.kraken.unit import test_kraken_connector_core_211 as base

if TYPE_CHECKING:
    from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway
    from market_connector.hb_compat.event_bus import EventBus
    from tests.exchanges.kraken.unit.test_kraken_connector_core_211 import Recorder

gateway = base.gateway
bus = base.bus
seen = base.seen

_LOGGER = "market_connector.exchanges.kraken.hb_compat.kraken_connector_core"
_GRACE = 60.0
_HARD = 6
_TXID = base._TXID
_MAX_IDS = 20
_WAIT = 30.0  # terminal_fill_wait_s default


class StrictRest(base.FakeKrakenRest):
    def _reply(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        reply = super()._reply(endpoint, body)
        if endpoint == "query_orders" and str(body.get("trades", "")).lower() not in {
            "true",
            "1",
        }:
            reply["result"] = {k: {**v, "trades": []} for k, v in reply["result"].items()}
        if endpoint == "query_trades" and len(str(body.get("txid", "")).split(",")) > _MAX_IDS:
            return {"error": ["EGeneral:Invalid arguments:txid"]}
        return reply


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def rest() -> StrictRest:
    return StrictRest()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def core(gateway: KrakenGateway, bus: EventBus, seen: Recorder, clock: FakeClock) -> Any:  # noqa: F811
    cls = importlib.import_module(base._CORE_MOD).KrakenConnectorCore
    c = cls(
        gateway,
        event_bus=bus,
        clock=clock,
        max_finished_orders=3,
        finished_order_grace_s=_GRACE,
        max_finished_orders_hard=_HARD,
    )
    await c.update_trading_rules()
    return c


def _trade(rest: StrictRest, tid: str, vol: str) -> None:
    cost = str(Decimal(vol) * 27000)
    rest.trades[tid] = base._trade_info(
        vol=vol, price="27000", cost=cost, fee="0.1", time=1700000000.5
    )


def _put(rest: StrictRest, **kw: Any) -> None:
    rest.orders[_TXID] = base._order_info(**kw)


def _terminals(seen: Recorder) -> int:
    return sum(len(seen.of(i)) for i in (base.BUY_COMPLETED, base.CANCELLED, base.FAILURE))


def _queries_for(rest: StrictRest, txid: str) -> int:
    return sum(1 for b in rest.bodies("query_orders") if txid in str(b["txid"]).split(","))


async def _underfilled_closed(core: Any, rest: StrictRest, seen: Recorder, status: str) -> str:
    """Order placed; poll sees ``status`` with vol_exec 0.5 but only trade T1 (0.2) listed."""
    cid = await base._place(core, seen)
    _trade(rest, "T1", "0.25")
    _trade(rest, "T2", "0.25")
    _put(rest, status=status, vol_exec="0.5", cost="13500", price="27000", trades=("T1",))
    await core._poll_once()
    return cid


def _order_of(seen: Recorder, *ids: int) -> list[int]:
    return [e for e, _ in seen.events if e in ids]


async def test_query_orders_requests_trades(core: Any, rest: StrictRest, seen: Recorder) -> None:
    await base._place(core, seen)
    _put(rest, status="open")
    await core._poll_once()
    bodies = rest.bodies("query_orders")
    assert bodies
    assert all(str(b.get("trades", "")).lower() in {"true", "1"} for b in bodies)


async def test_rest_books_fills_then_completes(core: Any, rest: StrictRest, seen: Recorder) -> None:
    cid = await base._place(core, seen)
    _trade(rest, "T1", "0.2")
    _trade(rest, "T2", "0.3")
    _put(rest, status="closed", vol_exec="0.5", cost="13500", price="27000", trades=("T1", "T2"))
    await core._poll_once()

    assert [e for e, _ in seen.events if e in (base.FILLED, base.BUY_COMPLETED)] == [
        base.FILLED,
        base.FILLED,
        base.BUY_COMPLETED,
    ]
    assert {f.order_id for f in seen.of(base.FILLED)} == {cid}
    assert seen.of(base.BUY_COMPLETED)[0].base_asset_amount == Decimal("0.5")


async def test_query_trades_chunked_to_20(core: Any, rest: StrictRest, seen: Recorder) -> None:
    await base._place(core, seen)
    ids = tuple(f"T-{i:02d}" for i in range(45))
    for tid in ids:
        _trade(rest, tid, "0.01")
    _put(rest, status="open", vol_exec="0.45", cost="12150", price="27000", trades=ids)
    await core._poll_once()

    calls = [str(b["txid"]).split(",") for b in rest.bodies("query_trades")]
    assert len(calls) == 3
    assert all(len(c) <= _MAX_IDS for c in calls)
    assert len(seen.of(base.FILLED)) == 45


async def test_reconcile_books_missing_trade_without_second_terminal(
    core: Any, rest: StrictRest, seen: Recorder
) -> None:
    cid = await _underfilled_closed(core, rest, seen, "closed")
    assert len(seen.of(base.FILLED)) == 1
    assert _terminals(seen) == 0  # held: fills first, terminal last
    held = core.fetch_order(cid)
    assert held is not None
    assert held.is_done is False
    assert held.executed_amount_base == Decimal("0.25")

    _put(rest, status="closed", vol_exec="0.5", cost="13500", price="27000", trades=("T1", "T2"))
    await core._poll_once()
    assert [f.exchange_trade_id for f in seen.of(base.FILLED)] == ["T1", "T2"]
    assert _order_of(seen, base.FILLED, base.BUY_COMPLETED) == [
        base.FILLED,
        base.FILLED,
        base.BUY_COMPLETED,
    ]
    assert seen.of(base.BUY_COMPLETED)[0].base_asset_amount == Decimal("0.5")

    before = _queries_for(rest, _TXID)
    await core._poll_once()  # finished and fully booked: no longer queried
    assert _queries_for(rest, _TXID) == before
    assert _terminals(seen) == 1


async def test_reconcile_expiry_is_surfaced(
    core: Any,
    rest: StrictRest,
    seen: Recorder,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _underfilled_closed(core, rest, seen, "closed")
    clock.now += _WAIT - 1
    queried = _queries_for(rest, _TXID)
    await core._poll_once()  # within the wait: still held, still polled
    assert _queries_for(rest, _TXID) > queried
    assert _terminals(seen) == 0
    assert core.late_trade_drops == 0

    clock.now += 2
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        await core._poll_once()
    assert len(seen.of(base.BUY_COMPLETED)) == 1
    assert seen.of(base.BUY_COMPLETED)[0].base_asset_amount == Decimal("0.25")
    assert core.late_trade_drops == 1
    assert any(r.levelno == logging.WARNING and _TXID in r.getMessage() for r in caplog.records)

    after = _queries_for(rest, _TXID)
    await core._poll_once()
    assert _queries_for(rest, _TXID) == after
    assert core.late_trade_drops == 1
    assert _terminals(seen) == 1


async def test_underfilled_cancel_reconciles(core: Any, rest: StrictRest, seen: Recorder) -> None:
    await base._place(core, seen)
    _trade(rest, "T1", "0.1")
    _trade(rest, "T2", "0.1")
    _put(rest, status="canceled", vol_exec="0.2", cost="5400", price="27000", trades=("T1",))
    await core._poll_once()
    assert len(seen.of(base.FILLED)) == 1
    assert _terminals(seen) == 0  # held until the missing trade is booked

    _put(rest, status="canceled", vol_exec="0.2", cost="5400", price="27000", trades=("T1", "T2"))
    await core._poll_once()
    assert [f.exchange_trade_id for f in seen.of(base.FILLED)] == ["T1", "T2"]
    assert _order_of(seen, base.FILLED, base.CANCELLED) == [
        base.FILLED,
        base.FILLED,
        base.CANCELLED,
    ]
    assert _terminals(seen) == 1


async def test_cancel_expiry_emits_cancelled_with_warning(
    core: Any,
    rest: StrictRest,
    seen: Recorder,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await base._place(core, seen)
    _trade(rest, "T1", "0.1")
    _put(rest, status="canceled", vol_exec="0.2", cost="5400", price="27000", trades=("T1",))
    await core._poll_once()
    assert _terminals(seen) == 0
    clock.now += _WAIT + 1
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        await core._poll_once()
    assert len(seen.of(base.CANCELLED)) == 1
    assert core.late_trade_drops == 1
    assert any(r.levelno == logging.WARNING and _TXID in r.getMessage() for r in caplog.records)


async def test_fully_booked_closed_completes_without_hold(
    core: Any, rest: StrictRest, seen: Recorder
) -> None:
    await base._place(core, seen)
    _trade(rest, "T1", "0.25")
    _trade(rest, "T2", "0.25")
    _put(rest, status="closed", vol_exec="0.5", cost="13500", price="27000", trades=("T1", "T2"))
    await core._poll_once()
    assert len(seen.of(base.BUY_COMPLETED)) == 1  # same poll, no hold
    assert core.late_trade_drops == 0


async def test_cancel_during_hold_is_noop(
    core: Any, rest: StrictRest, seen: Recorder, caplog: pytest.LogCaptureFixture
) -> None:
    cid = await _underfilled_closed(core, rest, seen, "closed")
    assert _terminals(seen) == 0
    assert core.fetch_order(cid).executed_amount_base == Decimal("0.25")

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        core.cancel(base._PAIR, cid)
        for _ in range(5):
            await asyncio.sleep(0)
    assert rest.bodies("cancel_order") == []
    assert seen.of(base.CANCELLED) == []
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    _put(rest, status="closed", vol_exec="0.5", cost="13500", price="27000", trades=("T1", "T2"))
    await core._poll_once()
    assert core.fetch_order(cid).executed_amount_base == Decimal("0.5")
    assert len(seen.of(base.BUY_COMPLETED)) == 1
    assert seen.of(base.CANCELLED) == []
    assert rest.bodies("cancel_order") == []


async def test_ws_and_rest_same_trade_booked_once(
    core: Any, rest: StrictRest, seen: Recorder
) -> None:
    await base._place(core, seen)
    _trade(rest, "T1", "0.2")
    _put(rest, status="open", vol_exec="0.2", cost="5400", price="27000", trades=("T1",))
    await core._poll_once()  # REST books T1
    assert len(seen.of(base.FILLED)) == 1

    core.on_own_trades(
        base._ws_trades("T1", vol="0.2", price="27000", cost="5400", fee="0.1", time="1700000000.5")
    )
    await core._poll_once()
    assert len(seen.of(base.FILLED)) == 1


async def test_hold_is_bounded_by_wait(
    core: Any, rest: StrictRest, seen: Recorder, clock: FakeClock
) -> None:
    n = 10
    for i in range(1, n + 1):
        core.buy(base._PAIR, base._AMOUNT, base.OrderType.LIMIT, base._PRICE)
        await seen.wait(base.BUY_CREATED, i)
        rest.orders[f"OTXID-{i}"] = base._order_info(
            status="closed", vol_exec="0.5", cost="13500", price="27000"
        )
    await core._poll_once()
    assert _terminals(seen) == 0  # all held: booked 0 < vol_exec

    clock.now += _WAIT + 1
    await core._poll_once()
    assert len(seen.of(base.BUY_COMPLETED)) == n
