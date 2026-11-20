"""Late-fill retention and surfacing (issue #284), RED step.

Pinned API on ``KrakenConnectorCore``:

* kwargs ``max_finished_orders=1000``, ``finished_order_grace_s=600.0``,
  ``max_finished_orders_hard=10_000``: a finished order is evicted only when
  ``len(_finished) > max_finished_orders`` AND it finished >= grace seconds ago (injected
  ``clock``); past the hard cap it is evicted regardless of age.
* ``late_trade_drops`` (int property): late WS trades that could not be booked (evicted
  order tombstone, or PendingTrades expiry on the injected clock); each logs a WARNING that
  contains the exchange txid.
"""

from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING, Any

import pytest

from market_connector.exchanges.kraken.schemas.ws import OwnTradesEvent
from tests.exchanges.kraken.unit import test_kraken_connector_core_211 as base

if TYPE_CHECKING:
    from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway
    from market_connector.hb_compat.event_bus import EventBus
    from tests.exchanges.kraken.unit.test_kraken_connector_core_211 import Recorder

# Reuse the #211 fixtures (REST fake, gateway, bus, recorder).
rest = base.rest
gateway = base.gateway
bus = base.bus
seen = base.seen

_LOGGER = "market_connector.exchanges.kraken.hb_compat.kraken_connector_core"
_MAX, _GRACE, _HARD = 3, 60.0, 6
_PENDING_TTL = 60.0


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _trade(
    txid: str, trade_id: str, *, vol: str = "0.5", seq: int = 1, cost: str = "13500.0"
) -> OwnTradesEvent:
    detail = {
        "ordertxid": txid,
        "pair": "XBT/USD",
        "time": "1700000000.5",
        "type": "buy",
        "ordertype": "limit",
        "price": "27000.0",
        "cost": cost,
        "fee": "1.0",
        "vol": vol,
        "margin": "0",
    }
    return OwnTradesEvent.from_raw_frame([[{trade_id: detail}], "ownTrades", {"sequence": seq}])


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
        max_finished_orders=_MAX,
        finished_order_grace_s=_GRACE,
        max_finished_orders_hard=_HARD,
    )
    await c.update_trading_rules()
    return c


async def _finish_one(core: Any, seen: Recorder, n: int) -> tuple[str, str]:
    """Place order #n (txid OTXID-n) and fully fill it over WS. Returns (cid, txid)."""
    cid = await base._place(core, seen)
    txid = f"OTXID-{n}"
    assert core.client_order_id_for_txid(txid) == cid
    core.on_own_trades(_trade(txid, f"TF-{n}"))
    assert core.fetch_order(cid).is_open is False
    return cid, txid


def _terminals(seen: Recorder) -> int:
    return sum(
        len(seen.of(i))
        for i in (base.BUY_COMPLETED, base.SELL_COMPLETED, base.CANCELLED, base.FAILURE)
    )


async def test_booked_late_fill_on_retained_order(core: Any, seen: Recorder) -> None:
    finished = [await _finish_one(core, seen, n) for n in range(1, _MAX + 3)]
    oldest_cid, oldest_txid = finished[0]
    terminals_before = _terminals(seen)
    fills_before = len(seen.of(base.FILLED))

    core.on_own_trades(_trade(oldest_txid, "LATE-1", vol="0.1", cost="2700.0", seq=2))

    assert core.fetch_order(oldest_cid) is not None
    new_fills = seen.of(base.FILLED)[fills_before:]
    assert [f.order_id for f in new_fills] == [oldest_cid]
    assert new_fills[0].exchange_trade_id == "LATE-1"
    assert _terminals(seen) == terminals_before
    assert core.late_trade_drops == 0


async def test_evicted_order_late_trade_is_surfaced(
    core: Any, seen: Recorder, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    oldest_cid, oldest_txid = await _finish_one(core, seen, 1)
    clock.now += _GRACE + 1
    for n in range(2, _MAX + 4):
        await _finish_one(core, seen, n)
    assert core.fetch_order(oldest_cid) is None  # evicted: past count AND grace

    fills_before = len(seen.of(base.FILLED))
    terminals_before = _terminals(seen)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        core.on_own_trades(_trade(oldest_txid, "LATE-2", vol="0.1", cost="2700.0", seq=2))

    assert core.late_trade_drops == 1
    assert any(
        r.levelno == logging.WARNING and oldest_txid in r.getMessage() for r in caplog.records
    )
    assert len(seen.of(base.FILLED)) == fills_before
    assert _terminals(seen) == terminals_before


async def test_pending_trade_expiry_is_surfaced(
    core: Any, seen: Recorder, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    core.on_own_trades(_trade("OUNKNOWN-1", "PT-1", vol="0.1", cost="2700.0"))
    assert core.late_trade_drops == 0
    clock.now += _PENDING_TTL + 1
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        core.on_own_trades(_trade("OUNKNOWN-2", "PT-2", vol="0.1", cost="2700.0", seq=2))

    assert core.late_trade_drops == 1
    assert any(
        r.levelno == logging.WARNING and "OUNKNOWN-1" in r.getMessage() for r in caplog.records
    )
    assert seen.of(base.FILLED) == []


async def test_trade_before_txid_known_books_within_ttl(
    core: Any, seen: Recorder, clock: FakeClock
) -> None:
    cid = core.buy(base._PAIR, base._AMOUNT, base.OrderType.LIMIT, base._PRICE)
    # Trade arrives before AddOrder returns the txid (no await yet: add_order has not run).
    core.on_own_trades(_trade("OTXID-1", "RACE-1"))
    clock.now += _PENDING_TTL / 2
    await seen.wait(base.BUY_CREATED)

    (fill,) = seen.of(base.FILLED)
    assert fill.order_id == cid
    assert core.late_trade_drops == 0


async def test_finished_store_is_finitely_bounded(
    core: Any, seen: Recorder, clock: FakeClock
) -> None:
    for n in range(1, 51):
        await _finish_one(core, seen, n)
        clock.now += 0.1  # always well within grace
        assert len(core._finished) <= _HARD
    assert len(core._finished) == _HARD
    # Hard-cap evictions leave a bounded tombstone structure behind.
    tombstones = getattr(core, "_evicted_txids", None)
    if tombstones is not None:
        assert len(tombstones) <= 10 * _HARD


async def test_age_gate_keeps_young_orders_past_count_cap(
    core: Any, seen: Recorder, clock: FakeClock
) -> None:
    cids = []
    for n in range(1, _MAX + 3):
        cids.append((await _finish_one(core, seen, n))[0])
    assert all(core.fetch_order(c) is not None for c in cids)  # within grace: all retained
    clock.now += _GRACE + 1
    cids.append((await _finish_one(core, seen, _MAX + 3))[0])
    assert len(core._finished) <= _MAX + 1
    assert core.fetch_order(cids[0]) is None


async def test_contract_unchanged_int_ids_and_no_terminal_on_late_fill(
    core: Any, seen: Recorder, bus: EventBus
) -> None:
    events = importlib.import_module(base._EVENTS_MOD)
    assert all(isinstance(i, int) for i in events.MARKET_EVENT_IDS.values())
    cid, txid = await _finish_one(core, seen, 1)
    before = _terminals(seen)
    core.on_own_trades(_trade(txid, "LATE-3", vol="0.1", cost="2700.0", seq=2))
    assert [eid for eid, _ in seen.events if eid not in base._ALL_IDS] == []
    assert len(seen.of(base.FILLED)) == 2
    assert _terminals(seen) == before
    assert core.fetch_order(cid) is not None
