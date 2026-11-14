"""Kraken connector core (issue #211, core-only scope), RED step.

Pinned public API (nothing here exists yet):

* ``market_connector.exchanges.kraken.hb_compat.kraken_connector_core``
    - ``KrakenConnectorCore(gateway, *, event_bus)``; pure Python, no hummingbot.
    - SYNC order API (ExchangePyBase parity); the AddOrder/CancelOrder run in the
      background and the outcome is observed through ``event_bus``:
        ``buy/sell(trading_pair, amount, order_type=LIMIT, price=Decimal("NaN"), **kwargs)
        -> client_order_id`` (kwargs ``expiration_ts`` / ``position_action`` are
        accepted and NOT forwarded); the order is tracked (PENDING_CREATE) before the
        call returns.
        ``cancel(trading_pair, client_order_id) -> client_order_id``.
    - ``fetch_order(cid) -> InFlightOrder | None`` (finished orders stay retrievable
      with ``is_open`` False but leave ``in_flight_orders``); ``in_flight_orders: dict``.
    - ``client_order_id_for_txid`` / ``txid_for_client_order_id``.
    - ``await update_order_status(cid)`` (REST QueryOrders then QueryTrades; the order
      STATUS drives completion), ``on_own_trades(OwnTradesEvent)`` (sync WS handler).
    - SYNC cache-backed reads: ``get_price_by_type(pair, PriceType) -> Decimal`` (NaN when
      unknown/empty) filled by ``await update_prices([pairs])``; ``get_order_book(pair)``
      (None before any fetch) filled by ``await update_order_book(pair)``.
    - ``KrakenConnectorCore(gateway, *, event_bus, status_poll_interval=10.0)``;
      ``await start()`` / ``await stop()`` run/cancel the status-polling task.
    - ``trading_rules: dict[str, rule]`` (``.min_order_size``, ``.min_notional_size``)
      filled by ``await update_trading_rules()``; ``quantize_order_amount(pair, amount)``.
    - ``get_balance(asset)`` / ``get_available_balance(asset)`` (hummingbot asset
      names) filled by ``await update_balances()`` (REST BalanceEx, endpoint name
      ``balance_ex``).
    - ``KRAKEN_ORDER_STATE_MAP: Mapping[KrakenOrderState, OrderState]``
* ``market_connector.hb_compat.order_events``: 7 plain dataclasses + ``MARKET_EVENT_IDS``.

Only the REST transport is faked; everything else runs for real. Events are read off a
real ``EventBus`` under hummingbot's MarketEvent integer ids.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import importlib
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from data_type_primitives.common import OrderType, PositionAction, PriceType, TradeType
from data_type_primitives.in_flight_order import InFlightOrder, OrderState
from data_type_primitives.trade_fee import AddedToCostTradeFee, TradeFeeBase

from market_connector.exchanges.kraken.hb_compat.kraken_startup_cleanup import (
    reconcile_stale_orders,
)
from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway
from market_connector.exchanges.kraken.schemas.enums import KrakenOrderState
from market_connector.exchanges.kraken.schemas.ws import OwnTradesEvent
from market_connector.hb_compat.event_bus import EventBus
from market_connector.transport.response import Response

_CORE_MOD = "market_connector.exchanges.kraken.hb_compat.kraken_connector_core"
_EVENTS_MOD = "market_connector.hb_compat.order_events"

BUY_COMPLETED, SELL_COMPLETED, CANCELLED, FILLED, FAILURE = 102, 103, 106, 107, 198
BUY_CREATED, SELL_CREATED = 200, 201
_ALL_IDS = (BUY_COMPLETED, SELL_COMPLETED, CANCELLED, FILLED, FAILURE, BUY_CREATED, SELL_CREATED)

_PAIR = "BTC-USD"
# Wire pair = AssetPairs ``altname``. Kraken REJECTS 'XBTZUSD' (the symbol mapper's
# output); the core builds its pair map from AssetPairs, never from the mapper.
_WIRE_PAIR = "XBTUSD"
_AMOUNT = Decimal("0.5")
_PRICE = Decimal("27000")
_NAN = Decimal("NaN")
_TXID = "OTXID-1"


def _events(name: str) -> Any:
    return getattr(importlib.import_module(_EVENTS_MOD), name)


# ---------------------------------------------------------------------------
# Fake REST transport (the only fake)
# ---------------------------------------------------------------------------


class FakeKrakenRest:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.add_order_errors: list[str] = []
        self.orders: dict[str, dict[str, Any]] = {}
        self.trades: dict[str, dict[str, Any]] = {}
        self.trades_error: list[str] = []
        self.open_orders: dict[str, dict[str, Any]] = {}
        self.ticker: dict[str, Any] = {
            "a": ["27010.0", "1", "1.000"],
            "b": ["27000.0", "1", "1.000"],
            "c": ["27003.5", "0.1"],
        }
        self.asset_pairs: dict[str, Any] = {
            "XXBTZUSD": {
                "altname": "XBTUSD",
                "wsname": "XBT/USD",
                "base": "XXBT",
                "quote": "ZUSD",
                "pair_decimals": 1,
                "lot_decimals": 8,
                "ordermin": "0.0001",
                "costmin": "0.5",
            },
            "XETHZUSD": {
                "altname": "ETHUSD",
                "wsname": "ETH/USD",
                "base": "XETH",
                "quote": "ZUSD",
                "pair_decimals": 2,
                "lot_decimals": 4,
                "ordermin": "0.02",
            },
            "SOLUSD": {
                "altname": "SOLUSD",
                "wsname": "SOL/USD",
                "base": "SOL",
                "quote": "ZUSD",
                "pair_decimals": 2,
                "lot_decimals": 2,
                "ordermin": "0.2",
                "costmin": "5",
            },
        }
        self.balance_ex: dict[str, Any] = {
            "XXBT": {"balance": "1.5", "credit": "0", "credit_used": "0", "hold_trade": "0.2"},
            "ZUSD": {"balance": "1000.5", "credit": "0", "credit_used": "0", "hold_trade": "0"},
        }
        self.query_orders_failures = 0  # next N QueryOrders calls raise a transport error
        self._n = 0

    async def request(
        self,
        endpoint_name: str,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        **_: Any,
    ) -> Response:
        body = dict(data or params or {})
        self.calls.append((endpoint_name, body))
        if endpoint_name == "query_orders" and self.query_orders_failures > 0:
            self.query_orders_failures -= 1
            raise httpx.ConnectError("transport down")
        return Response(
            raw=self._reply(endpoint_name, body),
            status_code=200,
            headers=httpx.Headers(),
            _endpoint=endpoint_name,
        )

    def bodies(self, endpoint: str) -> list[dict[str, Any]]:
        return [b for name, b in self.calls if name == endpoint]

    def _reply(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        ids = str(body.get("txid", "")).split(",")
        if endpoint == "add_order":
            if self.add_order_errors:
                return {"error": list(self.add_order_errors)}
            self._n += 1
            return {
                "error": [],
                "result": {"descr": {"order": "x"}, "txid": [f"OTXID-{self._n}"]},
            }
        if endpoint == "cancel_order":
            return {"error": [], "result": {"count": 1}}
        if endpoint == "query_orders":
            return {"error": [], "result": {t: self.orders[t] for t in ids if t in self.orders}}
        if endpoint == "query_trades":
            if self.trades_error:
                return {"error": list(self.trades_error)}
            return {"error": [], "result": {t: self.trades[t] for t in ids if t in self.trades}}
        if endpoint == "open_orders":
            return {"error": [], "result": {"open": self.open_orders}}
        if endpoint == "ticker":
            return {"error": [], "result": {"XXBTZUSD": self.ticker}}
        if endpoint == "depth":
            return {
                "error": [],
                "result": {
                    "XXBTZUSD": {
                        "asks": [["27010.0", "1.5", "1700000000"]],
                        "bids": [["27000.0", "2.0", "1700000000"]],
                    }
                },
            }
        if endpoint == "asset_pairs":
            return {"error": [], "result": self.asset_pairs}
        if endpoint == "balance_ex":
            return {"error": [], "result": self.balance_ex}
        raise AssertionError(f"unexpected endpoint {endpoint}")


def _order_info(
    *,
    status: str,
    vol_exec: str = "0",
    cost: str = "0",
    price: str = "0",
    fee: str = "0",
    ordertype: str = "limit",
    side: str = "buy",
    trades: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "status": status,
        "opentm": 1700000000.0,
        "vol": "0.5",
        "vol_exec": vol_exec,
        "cost": cost,
        "fee": fee,
        "price": price,
        "trades": list(trades),
        "descr": {
            "pair": "XXBTZUSD",  # replies name the pair differently: map by txid, not pair
            "type": side,
            "ordertype": ordertype,
            "price": "27000",
            "price2": "0",
            "leverage": "none",
            "order": "x",
        },
    }


def _trade_info(
    *, vol: str, price: str, cost: str, fee: str, time: float, side: str = "buy"
) -> dict[str, Any]:
    return {
        "ordertxid": _TXID,
        "pair": "XXBTZUSD",
        "time": time,  # REST: float
        "type": side,
        "ordertype": "limit",
        "price": price,
        "cost": cost,
        "fee": fee,
        "vol": vol,
        "margin": "0",
    }


def _ws_trades(
    trade_id: str,
    *,
    vol: str,
    price: str,
    cost: str,
    fee: str,
    time: str,
    side: str = "buy",
    seq: int = 1,
) -> OwnTradesEvent:
    detail = {
        "ordertxid": _TXID,
        "pair": "XBT/USD",
        "time": time,  # Kraken WS delivers the time as a STRING
        "type": side,
        "ordertype": "limit",
        "price": price,
        "cost": cost,
        "fee": fee,
        "vol": vol,
        "margin": "0",
    }
    return OwnTradesEvent.from_raw_frame([[{trade_id: detail}], "ownTrades", {"sequence": seq}])


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


class Recorder:
    """Collects bus events by MarketEvent id; waits on an asyncio.Event (no sleeping)."""

    def __init__(self, bus: EventBus) -> None:
        self.events: list[tuple[int, Any]] = []
        self._changed = asyncio.Event()
        for event_id in _ALL_IDS:
            bus.subscribe(event_id, lambda payload, eid=event_id: self._record(eid, payload))  # type: ignore[arg-type]

    def _record(self, event_id: int, payload: Any) -> None:
        self.events.append((event_id, payload))
        self._changed.set()

    def of(self, event_id: int) -> list[Any]:
        return [payload for eid, payload in self.events if eid == event_id]

    async def wait(self, event_id: int, count: int = 1, timeout: float = 2.0) -> None:
        async def _until() -> None:
            while len(self.of(event_id)) < count:
                self._changed.clear()
                await self._changed.wait()

        await asyncio.wait_for(_until(), timeout)


@pytest.fixture
def rest() -> FakeKrakenRest:
    return FakeKrakenRest()


@pytest.fixture
def gateway(rest: FakeKrakenRest) -> KrakenGateway:
    gw = KrakenGateway(api_key="k", secret_key="c2VjcmV0")
    gw._rest = rest  # type: ignore[assignment]
    gw._started = True
    return gw


@pytest.fixture
def bus() -> EventBus:
    return EventBus(name="core-211")


@pytest.fixture
def seen(bus: EventBus) -> Recorder:
    return Recorder(bus)


@pytest.fixture
def bare_connector(gateway: KrakenGateway, bus: EventBus, seen: Recorder) -> Any:
    """A core whose trading rules / pair map are NOT loaded yet."""
    cls = importlib.import_module(_CORE_MOD).KrakenConnectorCore
    return cls(gateway, event_bus=bus)


@pytest.fixture
async def connector(bare_connector: Any) -> Any:
    await bare_connector.update_trading_rules()  # builds the hb pair -> altname map
    return bare_connector


@pytest.fixture
def make_connector(gateway: KrakenGateway, bus: EventBus, seen: Recorder) -> Any:
    """Async factory: a rules-loaded core with an injected ``status_poll_interval``."""
    cls = importlib.import_module(_CORE_MOD).KrakenConnectorCore

    async def _make(interval: float) -> Any:
        core = cls(gateway, event_bus=bus, status_poll_interval=interval)
        await core.update_trading_rules()
        return core

    return _make


async def _place(
    connector: Any,
    seen: Recorder,
    side: str = "buy",
    order_type: OrderType = OrderType.LIMIT,
    price: Decimal = _PRICE,
) -> str:
    """Sync buy/sell, then wait for the Created event (AddOrder returned a txid)."""
    method = connector.buy if side == "buy" else connector.sell
    client_id = method(
        trading_pair=_PAIR,
        amount=_AMOUNT,
        order_type=order_type,
        price=price,
        position_action=PositionAction.NIL,
    )
    await seen.wait(BUY_CREATED if side == "buy" else SELL_CREATED)
    return client_id


# ---------------------------------------------------------------------------
# 1. buy / sell (sync API, background AddOrder)
# ---------------------------------------------------------------------------


class TestPlacement:
    async def test_buy_is_sync_and_tracks_before_any_request(
        self, connector: Any, rest: FakeKrakenRest
    ) -> None:
        client_id = connector.buy(_PAIR, _AMOUNT, OrderType.LIMIT, _PRICE)
        assert isinstance(client_id, str)
        assert client_id
        assert rest.bodies("add_order") == []  # AddOrder is scheduled, not yet run
        order = connector.fetch_order(client_id)
        assert isinstance(order, InFlightOrder)
        assert order.is_open is True
        assert order.exchange_order_id is None

    async def test_buy_sends_add_order_and_stores_txid(
        self, connector: Any, rest: FakeKrakenRest, seen: Recorder
    ) -> None:
        client_id = await _place(connector, seen)
        assert rest.bodies("add_order") == [
            {
                "pair": _WIRE_PAIR,
                "type": "buy",
                "ordertype": "limit",
                "volume": "0.5",
                "price": "27000",
            }
        ]
        order = connector.fetch_order(client_id)
        assert order.client_order_id == client_id
        assert order.exchange_order_id == _TXID
        assert order.trading_pair == _PAIR
        assert order.trade_type is TradeType.BUY
        assert order.order_type is OrderType.LIMIT
        assert order.amount == _AMOUNT
        assert order.price == _PRICE
        assert order.is_open is True

    async def test_sell_sends_add_order(
        self, connector: Any, rest: FakeKrakenRest, seen: Recorder
    ) -> None:
        client_id = await _place(connector, seen, "sell")
        assert isinstance(client_id, str)
        (body,) = rest.bodies("add_order")
        assert body["type"] == "sell"
        assert body["ordertype"] == "limit"
        assert connector.fetch_order(client_id).trade_type is TradeType.SELL

    async def test_wire_pair_is_altname_from_asset_pairs(
        self, connector: Any, rest: FakeKrakenRest, seen: Recorder
    ) -> None:
        connector.sell("SOL-USD", Decimal("1"), OrderType.LIMIT, Decimal("150"))
        await seen.wait(SELL_CREATED)
        assert rest.bodies("add_order")[0]["pair"] == "SOLUSD"
        assert _WIRE_PAIR == "XBTUSD"

    async def _assert_fails_cleanly(
        self, core: Any, pair: str, rest: FakeKrakenRest, seen: Recorder
    ) -> None:
        client_id = core.buy(pair, _AMOUNT, OrderType.LIMIT, _PRICE)  # no exception
        assert isinstance(client_id, str)
        await seen.wait(FAILURE)
        assert rest.bodies("add_order") == []
        assert [eid for eid, _ in seen.events] == [FAILURE]
        assert seen.of(FAILURE)[0].order_id == client_id
        order = core.fetch_order(client_id)
        assert order is None or order.is_open is False
        assert client_id not in core.in_flight_orders

    async def test_rules_not_loaded_fails_without_raising(
        self, bare_connector: Any, rest: FakeKrakenRest, seen: Recorder
    ) -> None:
        await self._assert_fails_cleanly(bare_connector, _PAIR, rest, seen)

    async def test_unknown_pair_fails_without_raising(
        self, connector: Any, rest: FakeKrakenRest, seen: Recorder
    ) -> None:
        await self._assert_fails_cleanly(connector, "FOO-USD", rest, seen)

    async def test_default_order_type_is_limit(
        self, connector: Any, rest: FakeKrakenRest, seen: Recorder
    ) -> None:
        connector.buy(_PAIR, _AMOUNT, price=_PRICE)
        await seen.wait(BUY_CREATED)
        assert rest.bodies("add_order")[0]["ordertype"] == "limit"

    async def test_client_ids_are_unique(self, connector: Any) -> None:
        assert connector.buy(_PAIR, _AMOUNT, price=_PRICE) != connector.buy(
            _PAIR, _AMOUNT, price=_PRICE
        )

    async def test_expiration_ts_and_position_action_are_tolerated_not_forwarded(
        self, connector: Any, rest: FakeKrakenRest, seen: Recorder
    ) -> None:
        connector.buy(
            _PAIR,
            _AMOUNT,
            OrderType.LIMIT,
            _PRICE,
            expiration_ts=_NAN,
            position_action=PositionAction.OPEN,
        )
        await seen.wait(BUY_CREATED)
        assert rest.bodies("add_order") == [
            {
                "pair": _WIRE_PAIR,
                "type": "buy",
                "ordertype": "limit",
                "volume": "0.5",
                "price": "27000",
            }
        ]

    @pytest.mark.parametrize("price", [pytest.param(None, id="default_nan"), Decimal("27000")])
    async def test_market_accepts_nan_or_any_price(
        self, connector: Any, rest: FakeKrakenRest, seen: Recorder, price: Decimal | None
    ) -> None:
        if price is None:
            connector.buy(_PAIR, _AMOUNT, OrderType.MARKET)
        else:
            connector.buy(_PAIR, _AMOUNT, OrderType.MARKET, price)
        await seen.wait(BUY_CREATED)
        assert rest.bodies("add_order") == [
            {"pair": _WIRE_PAIR, "type": "buy", "ordertype": "market", "volume": "0.5"}
        ]


# ---------------------------------------------------------------------------
# 1b. Created events (200 / 201), emitted when AddOrder returns a txid
# ---------------------------------------------------------------------------


class TestCreatedEvents:
    async def test_buy_created(self, connector: Any, seen: Recorder) -> None:
        client_id = await _place(connector, seen)
        (created,) = seen.of(BUY_CREATED)
        assert seen.of(SELL_CREATED) == []
        assert created.order_id == client_id
        assert created.type is OrderType.LIMIT
        assert created.trading_pair == _PAIR
        assert created.amount == _AMOUNT
        assert created.price == _PRICE
        assert created.exchange_order_id == _TXID
        assert created.leverage == 1
        assert created.position == "NIL"
        assert isinstance(created.timestamp, float)
        assert isinstance(created.creation_timestamp, float)

    async def test_sell_created(self, connector: Any, seen: Recorder) -> None:
        client_id = await _place(connector, seen, "sell")
        (created,) = seen.of(SELL_CREATED)
        assert seen.of(BUY_CREATED) == []
        assert created.order_id == client_id
        assert created.exchange_order_id == _TXID


# ---------------------------------------------------------------------------
# 2. client id <-> txid map
# ---------------------------------------------------------------------------


class TestIdMap:
    async def test_both_directions(self, connector: Any, seen: Recorder) -> None:
        client_id = await _place(connector, seen)
        assert connector.txid_for_client_order_id(client_id) == _TXID
        assert connector.client_order_id_for_txid(_TXID) == client_id

    async def test_unknown_ids_map_to_none(self, connector: Any, seen: Recorder) -> None:
        await _place(connector, seen)
        assert connector.txid_for_client_order_id("nope") is None
        assert connector.client_order_id_for_txid("OUNKNOWN") is None

    async def test_unknown_order_is_none(self, connector: Any) -> None:
        assert connector.fetch_order("nope") is None


# ---------------------------------------------------------------------------
# 3. status map
# ---------------------------------------------------------------------------


class TestStatusMap:
    @pytest.mark.parametrize("state", list(KrakenOrderState))
    def test_every_kraken_state_maps_to_an_order_state(self, state: KrakenOrderState) -> None:
        state_map = importlib.import_module(_CORE_MOD).KRAKEN_ORDER_STATE_MAP
        assert isinstance(state_map[state], OrderState)

    def test_map_has_no_extra_keys(self) -> None:
        state_map = importlib.import_module(_CORE_MOD).KRAKEN_ORDER_STATE_MAP
        assert set(state_map) == set(KrakenOrderState)

    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            (KrakenOrderState.PENDING, OrderState.PENDING_CREATE),
            (KrakenOrderState.OPEN, OrderState.OPEN),
            (KrakenOrderState.CLOSED, OrderState.FILLED),
            (KrakenOrderState.CANCELED, OrderState.CANCELED),
            (KrakenOrderState.EXPIRED, OrderState.CANCELED),
        ],
    )
    def test_pinned_values(self, state: KrakenOrderState, expected: OrderState) -> None:
        state_map = importlib.import_module(_CORE_MOD).KRAKEN_ORDER_STATE_MAP
        assert state_map[state] is expected


# ---------------------------------------------------------------------------
# 4. fills: REST and WS -> OrderFilled(107), then Buy/SellOrderCompleted(102/103)
# ---------------------------------------------------------------------------


class TestRestFills:
    async def test_partial_then_full_buy(
        self, connector: Any, rest: FakeKrakenRest, seen: Recorder
    ) -> None:
        client_id = await _place(connector, seen)
        rest.trades["T1"] = _trade_info(
            vol="0.2", price="27000.0", cost="5400.0", fee="8.64", time=1700000000.5
        )
        rest.orders[_TXID] = _order_info(
            status="open", vol_exec="0.2", cost="5400.0", price="27000.0", trades=("T1",)
        )
        await connector.update_order_status(client_id)

        (fill,) = seen.of(FILLED)
        assert fill.order_id == client_id
        assert fill.trading_pair == _PAIR
        assert fill.trade_type is TradeType.BUY
        assert fill.order_type is OrderType.LIMIT
        assert fill.price == Decimal("27000.0")
        assert fill.amount == Decimal("0.2")
        assert isinstance(fill.trade_fee, TradeFeeBase)
        assert fill.trade_fee.flat_fees[0].amount == Decimal("8.64")
        assert fill.exchange_trade_id == "T1"
        assert fill.exchange_order_id == _TXID
        assert fill.leverage == 1
        assert fill.position == "NIL"
        assert isinstance(fill.timestamp, float)
        assert fill.timestamp == 1700000000.5
        assert seen.of(BUY_COMPLETED) == []
        assert connector.fetch_order(client_id).is_open is True

        rest.trades["T2"] = _trade_info(
            vol="0.3", price="27000.0", cost="8100.0", fee="12.96", time=1700000001.25
        )
        rest.orders[_TXID] = _order_info(
            status="closed",
            vol_exec="0.5",
            cost="13500.0",
            price="27000.0",
            trades=("T1", "T2"),
        )
        await connector.update_order_status(client_id)

        fills = seen.of(FILLED)
        assert [f.exchange_trade_id for f in fills] == ["T1", "T2"]  # T1 not re-emitted
        assert isinstance(fills[1].timestamp, float)
        assert fills[1].timestamp == 1700000001.25
        (done,) = seen.of(BUY_COMPLETED)
        assert seen.of(SELL_COMPLETED) == []
        assert done.order_id == client_id
        assert done.base_asset == "BTC"
        assert done.quote_asset == "USD"
        assert done.base_asset_amount == Decimal("0.5")
        assert done.quote_asset_amount == Decimal("13500.0")
        assert done.order_type is OrderType.LIMIT
        assert done.exchange_order_id == _TXID
        assert isinstance(done.timestamp, float)
        assert connector.fetch_order(client_id).is_open is False
        assert client_id not in connector.in_flight_orders
        ids = [eid for eid, _ in seen.events]
        assert ids.index(BUY_CREATED) < ids.index(FILLED)  # created precedes any fill
        assert ids.index(BUY_COMPLETED) > max(i for i, e in enumerate(ids) if e == FILLED)

    async def test_polling_again_is_idempotent(
        self, connector: Any, rest: FakeKrakenRest, seen: Recorder
    ) -> None:
        client_id = await _place(connector, seen, "sell")
        rest.trades["T1"] = _trade_info(
            vol="0.5", price="27000.0", cost="13500.0", fee="21.6", time=1700000000.5, side="sell"
        )
        rest.orders[_TXID] = _order_info(
            status="closed",
            vol_exec="0.5",
            cost="13500.0",
            price="27000.0",
            trades=("T1",),
            side="sell",
        )
        await connector.update_order_status(client_id)
        await connector.update_order_status(client_id)
        assert len(seen.of(FILLED)) == 1
        assert len(seen.of(SELL_COMPLETED)) == 1
        assert seen.of(BUY_COMPLETED) == []


class TestWsFills:
    async def test_partial_then_full_sell(self, connector: Any, seen: Recorder) -> None:
        client_id = await _place(connector, seen, "sell")
        connector.on_own_trades(
            _ws_trades(
                "T1",
                vol="0.2",
                price="27000.0",
                cost="5400.0",
                fee="8.64",
                time="1700000000.5123",
                side="sell",
            )
        )
        (fill,) = seen.of(FILLED)
        assert fill.order_id == client_id
        assert fill.trade_type is TradeType.SELL
        assert fill.amount == Decimal("0.2")
        assert fill.price == Decimal("27000.0")
        assert fill.exchange_trade_id == "T1"
        assert fill.exchange_order_id == _TXID
        assert fill.trade_fee.flat_fees[0].amount == Decimal("8.64")
        assert isinstance(fill.timestamp, float)  # hummingbot #8199: WS time is a str
        assert fill.timestamp == 1700000000.5123
        assert seen.of(SELL_COMPLETED) == []
        order = connector.fetch_order(client_id)
        assert order.is_open is True
        assert isinstance(order.order_fills["T1"].fill_timestamp, float)

        full = _ws_trades(
            "T2",
            vol="0.3",
            price="27000.0",
            cost="8100.0",
            fee="12.96",
            time="1700000002.75",
            side="sell",
            seq=2,
        )
        connector.on_own_trades(full)
        connector.on_own_trades(full)  # replayed frame must not double count

        fills = seen.of(FILLED)
        assert [f.exchange_trade_id for f in fills] == ["T1", "T2"]
        assert isinstance(fills[1].timestamp, float)
        assert fills[1].timestamp == 1700000002.75
        (done,) = seen.of(SELL_COMPLETED)
        assert seen.of(BUY_COMPLETED) == []
        assert done.order_id == client_id
        assert done.base_asset == "BTC"
        assert done.quote_asset == "USD"
        assert done.base_asset_amount == Decimal("0.5")
        assert done.quote_asset_amount == Decimal("13500.0")
        assert done.exchange_order_id == _TXID
        assert connector.fetch_order(client_id).is_open is False

    async def test_trade_for_unknown_order_is_ignored(self, connector: Any, seen: Recorder) -> None:
        await _place(connector, seen, "sell")
        before = list(seen.events)
        stranger = {
            "ordertxid": "OSTRANGER",
            "time": "1700000000.5",
            "vol": "0.1",
            "price": "1",
            "cost": "0.1",
            "fee": "0",
            "type": "buy",
        }
        connector.on_own_trades(
            OwnTradesEvent.from_raw_frame([[{"TX": stranger}], "ownTrades", {"sequence": 9}])
        )
        assert seen.events == before


# ---------------------------------------------------------------------------
# 5. triggered orders complete via order STATUS (QueryTrades errors)
# ---------------------------------------------------------------------------

_INVALID_ORDER_ID = ["EOrder:Invalid Order ID"]


class TestTriggeredOrders:
    @pytest.mark.parametrize(
        ("order_type", "kraken_type"),
        [
            pytest.param(OrderType.STOP_LOSS, "stop-loss", id="stop_loss"),
            pytest.param(OrderType.TAKE_PROFIT, "take-profit", id="take_profit"),
        ],
    )
    async def test_closed_status_completes_when_query_trades_errors(
        self,
        connector: Any,
        rest: FakeKrakenRest,
        seen: Recorder,
        order_type: OrderType,
        kraken_type: str,
    ) -> None:
        client_id = await _place(connector, seen, "sell", order_type, Decimal("26950.5"))
        (body,) = rest.bodies("add_order")
        assert body["ordertype"] == kraken_type
        assert body["price"] == "26950.5"

        rest.trades_error = _INVALID_ORDER_ID
        rest.orders[_TXID] = _order_info(
            status="closed",
            vol_exec="0.5",
            cost="13475.0",
            price="26950.0",
            ordertype=kraken_type,
            side="sell",
            trades=("T9",),
        )
        await connector.update_order_status(client_id)

        (done,) = seen.of(SELL_COMPLETED)
        assert done.order_id == client_id
        assert done.base_asset_amount == Decimal("0.5")
        assert done.quote_asset_amount == Decimal("13475.0")
        assert done.order_type is order_type
        assert done.exchange_order_id == _TXID
        assert seen.of(FAILURE) == []
        assert connector.fetch_order(client_id).is_open is False
        assert client_id not in connector.in_flight_orders
        for fill in seen.of(FILLED):  # a synthesized fill, if any, is well-formed
            assert isinstance(fill.timestamp, float)
            assert fill.order_id == client_id

    async def test_untriggered_order_stays_open_while_query_trades_errors(
        self, connector: Any, rest: FakeKrakenRest, seen: Recorder
    ) -> None:
        client_id = await _place(connector, seen, "sell", OrderType.STOP_LOSS, Decimal("26950.5"))
        before = list(seen.events)
        rest.trades_error = _INVALID_ORDER_ID
        rest.orders[_TXID] = _order_info(status="open", ordertype="stop-loss", side="sell")
        await connector.update_order_status(client_id)  # must not raise
        assert seen.events == before
        assert connector.fetch_order(client_id).is_open is True
        assert client_id in connector.in_flight_orders

    async def test_canceled_status_emits_cancelled(
        self, connector: Any, rest: FakeKrakenRest, seen: Recorder
    ) -> None:
        client_id = await _place(connector, seen, "sell", OrderType.TAKE_PROFIT, Decimal("30000"))
        rest.trades_error = _INVALID_ORDER_ID
        rest.orders[_TXID] = _order_info(status="canceled", ordertype="take-profit", side="sell")
        await connector.update_order_status(client_id)
        (cancelled,) = seen.of(CANCELLED)
        assert cancelled.order_id == client_id
        assert seen.of(SELL_COMPLETED) == []
        assert connector.fetch_order(client_id).is_open is False

    async def test_conditional_order_locks_no_balance(self, connector: Any, seen: Recorder) -> None:
        await connector.update_balances()
        before = connector.get_available_balance("BTC")
        await _place(connector, seen, "sell", OrderType.STOP_LOSS, Decimal("26950.5"))
        assert connector.get_available_balance("BTC") == before


# ---------------------------------------------------------------------------
# 6. cancel (sync, background CancelOrder)
# ---------------------------------------------------------------------------


class TestCancel:
    async def test_cancel_sends_request_and_emits_event(
        self, connector: Any, rest: FakeKrakenRest, seen: Recorder
    ) -> None:
        client_id = await _place(connector, seen)
        assert connector.cancel(_PAIR, client_id) == client_id  # sync, returns the id
        await seen.wait(CANCELLED)

        assert rest.bodies("cancel_order") == [{"txid": _TXID}]
        (cancelled,) = seen.of(CANCELLED)
        assert cancelled.order_id == client_id
        assert cancelled.exchange_order_id == _TXID
        assert isinstance(cancelled.timestamp, float)
        assert connector.fetch_order(client_id).is_open is False
        assert client_id not in connector.in_flight_orders

    async def test_cancel_unknown_order_returns_id_without_raising(self, connector: Any) -> None:
        assert connector.cancel(_PAIR, "nope") == "nope"


# ---------------------------------------------------------------------------
# 7. rejected AddOrder
# ---------------------------------------------------------------------------


class TestRejection:
    async def test_rejected_add_order_emits_failure(
        self, connector: Any, rest: FakeKrakenRest, seen: Recorder
    ) -> None:
        rest.add_order_errors = ["EOrder:Insufficient funds"]
        client_id = connector.buy(_PAIR, _AMOUNT, OrderType.LIMIT, _PRICE)  # no raise
        assert isinstance(client_id, str)
        await seen.wait(FAILURE)

        (failure,) = seen.of(FAILURE)
        assert failure.order_id == client_id
        assert failure.order_type is OrderType.LIMIT
        assert "Insufficient funds" in (failure.error_message or "")
        assert isinstance(failure.timestamp, float)
        assert [eid for eid, _ in seen.events] == [FAILURE]  # no Created event
        order = connector.fetch_order(client_id)
        assert order is None or order.is_open is False
        assert client_id not in connector.in_flight_orders


# ---------------------------------------------------------------------------
# 5b. market data: prices and order book
# ---------------------------------------------------------------------------


class TestMarketData:
    @pytest.mark.parametrize(
        ("price_type", "expected"),
        [
            (PriceType.MidPrice, Decimal("27005.0")),
            (PriceType.BestBid, Decimal("27000.0")),
            (PriceType.BestAsk, Decimal("27010.0")),
            (PriceType.LastTrade, Decimal("27003.5")),
        ],
    )
    async def test_get_price_by_type_is_sync_and_cache_backed(
        self, connector: Any, rest: FakeKrakenRest, price_type: PriceType, expected: Decimal
    ) -> None:
        await connector.update_prices([_PAIR])
        assert rest.bodies("ticker") == [{"pair": _WIRE_PAIR}]
        price = connector.get_price_by_type(_PAIR, price_type)  # sync, no await
        assert isinstance(price, Decimal)
        assert price == expected
        connector.get_price_by_type(_PAIR, price_type)
        assert len(rest.bodies("ticker")) == 1  # reads never hit REST

    @pytest.mark.parametrize("price_type", [PriceType.MidPrice, PriceType.LastTrade])
    def test_price_is_nan_with_empty_cache(self, connector: Any, price_type: PriceType) -> None:
        assert connector.get_price_by_type(_PAIR, price_type).is_nan()

    async def test_price_is_nan_for_unknown_pair(self, connector: Any) -> None:
        await connector.update_prices([_PAIR])
        assert connector.get_price_by_type("ETH-USD", PriceType.MidPrice).is_nan()

    async def test_get_order_book_is_sync_and_returns_last_fetched_snapshot(
        self, connector: Any, rest: FakeKrakenRest
    ) -> None:
        await connector.update_order_book(_PAIR)
        (request,) = rest.bodies("depth")
        assert request["pair"] == _WIRE_PAIR  # gateway gets the converted pair
        book = connector.get_order_book(_PAIR)  # sync, no await
        assert book.bids[0] == (Decimal("27000.0"), Decimal("2.0"))
        assert book.asks[0] == (Decimal("27010.0"), Decimal("1.5"))
        connector.get_order_book(_PAIR)
        assert len(rest.bodies("depth")) == 1  # reads never hit REST

    def test_get_order_book_before_any_fetch_returns_none(self, connector: Any) -> None:
        assert connector.get_order_book(_PAIR) is None


# ---------------------------------------------------------------------------
# 5e. built-in status polling (start/stop, status_poll_interval)
# ---------------------------------------------------------------------------


class TestStatusPolling:
    async def test_triggered_stop_loss_completes_without_explicit_update(
        self, make_connector: Any, rest: FakeKrakenRest, seen: Recorder
    ) -> None:
        core = await make_connector(0.01)
        await core.start()
        try:
            client_id = await _place(core, seen, "sell", OrderType.STOP_LOSS, Decimal("26950.5"))
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
            await seen.wait(SELL_COMPLETED)  # nobody calls update_order_status
        finally:
            await core.stop()
        (done,) = seen.of(SELL_COMPLETED)
        assert done.order_id == client_id
        assert done.base_asset_amount == Decimal("0.5")
        assert done.quote_asset_amount == Decimal("13475.0")
        assert core.fetch_order(client_id).is_open is False

    async def test_poll_error_is_survived(
        self, make_connector: Any, rest: FakeKrakenRest, seen: Recorder
    ) -> None:
        core = await make_connector(0.01)
        await core.start()
        try:
            await _place(core, seen, "sell", OrderType.TAKE_PROFIT, Decimal("30000"))
            rest.trades_error = _INVALID_ORDER_ID
            rest.orders[_TXID] = _order_info(
                status="closed",
                vol_exec="0.5",
                cost="15000.0",
                price="30000.0",
                ordertype="take-profit",
                side="sell",
                trades=("T9",),
            )
            rest.query_orders_failures = 3  # transport errors first, then the real answer
            await seen.wait(SELL_COMPLETED)
        finally:
            await core.stop()
        assert rest.query_orders_failures == 0  # the loop kept polling through the errors
        assert len(seen.of(SELL_COMPLETED)) == 1

    async def test_stop_cancels_the_task_cleanly(self, make_connector: Any) -> None:
        before = asyncio.all_tasks()
        core = await make_connector(0.01)
        await core.start()
        assert asyncio.all_tasks() - before  # a polling task is running
        await core.stop()
        assert asyncio.all_tasks() <= before  # nothing left pending
        await core.stop()  # idempotent

    async def test_stop_without_start_is_safe(self, make_connector: Any) -> None:
        core = await make_connector(0.01)
        await core.stop()

    async def test_no_polling_before_start(
        self, make_connector: Any, rest: FakeKrakenRest, seen: Recorder
    ) -> None:
        core = await make_connector(0.01)
        await _place(core, seen, "sell", OrderType.STOP_LOSS, Decimal("26950.5"))
        assert rest.bodies("query_orders") == []


# ---------------------------------------------------------------------------
# 5c. trading rules and amount quantization
# ---------------------------------------------------------------------------


class TestTradingRules:
    async def test_empty_until_updated(self, bare_connector: Any) -> None:
        assert bare_connector.trading_rules == {}

    async def test_update_populates_rules_keyed_by_hummingbot_pair(self, connector: Any) -> None:
        await connector.update_trading_rules()
        rules = connector.trading_rules
        assert isinstance(rules, dict)
        assert {"BTC-USD", "ETH-USD", "SOL-USD"} <= set(rules)
        assert rules["SOL-USD"].min_notional_size == Decimal(5)
        assert rules["BTC-USD"].min_order_size == Decimal("0.0001")
        assert rules["BTC-USD"].min_notional_size == Decimal("0.5")
        assert rules["ETH-USD"].min_order_size == Decimal("0.02")
        assert rules["ETH-USD"].min_notional_size == Decimal(0)  # costmin absent
        assert isinstance(rules["BTC-USD"].min_order_size, Decimal)

    @pytest.mark.parametrize(
        ("pair", "amount", "expected"),
        [
            ("BTC-USD", "0.123456789", "0.12345678"),  # lot_decimals=8, rounds DOWN
            ("ETH-USD", "1.23459", "1.2345"),  # lot_decimals=4
            ("BTC-USD", "0.00009999", "0"),  # below min_order_size
            ("ETH-USD", "0.0199", "0"),
            ("BTC-USD", "0.0001", "0.0001"),  # exactly the minimum passes
        ],
    )
    async def test_quantize_order_amount(
        self, connector: Any, pair: str, amount: str, expected: str
    ) -> None:
        await connector.update_trading_rules()
        result = connector.quantize_order_amount(pair, Decimal(amount))
        assert isinstance(result, Decimal)
        assert result == Decimal(expected)


# ---------------------------------------------------------------------------
# 5d. balances
# ---------------------------------------------------------------------------


class TestBalances:
    async def test_zero_until_updated(self, connector: Any) -> None:
        assert connector.get_balance("BTC") == Decimal(0)
        assert connector.get_available_balance("BTC") == Decimal(0)

    async def test_hummingbot_asset_names_and_hold_trade(self, connector: Any) -> None:
        await connector.update_balances()
        assert connector.get_balance("BTC") == Decimal("1.5")  # not 'XXBT'
        assert connector.get_available_balance("BTC") == Decimal("1.3")  # minus hold_trade
        assert connector.get_balance("USD") == Decimal("1000.5")
        assert connector.get_available_balance("USD") == Decimal("1000.5")
        assert connector.get_balance("DOGE") == Decimal(0)


# ---------------------------------------------------------------------------
# 8. event contract (comments 6881070158 + 6881520595): 7 classes, 56 fields
# ---------------------------------------------------------------------------

_M = dataclasses.MISSING
_COMPLETED_FIELDS: list[tuple[str, Any]] = [
    ("timestamp", _M),
    ("order_id", _M),
    ("base_asset", _M),
    ("quote_asset", _M),
    ("base_asset_amount", _M),
    ("quote_asset_amount", _M),
    ("order_type", _M),
    ("exchange_order_id", None),
]
_CREATED_FIELDS: list[tuple[str, Any]] = [
    ("timestamp", _M),
    ("type", _M),
    ("trading_pair", _M),
    ("amount", _M),
    ("price", _M),
    ("order_id", _M),
    ("creation_timestamp", _M),
    ("exchange_order_id", None),
    ("leverage", 1),
    ("position", PositionAction.NIL.value),
]
_CONTRACT: dict[str, list[tuple[str, Any]]] = {
    "BuyOrderCompletedEvent": _COMPLETED_FIELDS,
    "SellOrderCompletedEvent": _COMPLETED_FIELDS,
    "OrderCancelledEvent": [("timestamp", _M), ("order_id", _M), ("exchange_order_id", None)],
    "OrderFilledEvent": [
        ("timestamp", _M),
        ("order_id", _M),
        ("trading_pair", _M),
        ("trade_type", _M),
        ("order_type", _M),
        ("price", _M),
        ("amount", _M),
        ("trade_fee", _M),
        ("exchange_trade_id", ""),
        ("exchange_order_id", ""),
        ("leverage", 1),
        ("position", PositionAction.NIL.value),
    ],
    "MarketOrderFailureEvent": [
        ("timestamp", _M),
        ("order_id", _M),
        ("order_type", _M),
        ("error_message", None),
        ("error_type", None),
    ],
    "BuyOrderCreatedEvent": _CREATED_FIELDS,
    "SellOrderCreatedEvent": _CREATED_FIELDS,
}


class TestEventContract:
    def test_contract_totals_56_fields_in_7_classes(self) -> None:
        assert len(_CONTRACT) == 7
        assert sum(len(v) for v in _CONTRACT.values()) == 56

    @pytest.mark.parametrize("name", list(_CONTRACT))
    def test_fields_and_defaults_match_exactly(self, name: str) -> None:
        cls = _events(name)
        assert dataclasses.is_dataclass(cls)
        actual = [(f.name, f.default) for f in dataclasses.fields(cls)]
        assert actual == _CONTRACT[name]  # names, declaration order, defaults
        for f in dataclasses.fields(cls):
            assert f.default_factory is _M

    @pytest.mark.parametrize("name", ["BuyOrderCreatedEvent", "SellOrderCreatedEvent"])
    def test_created_events_have_seven_required_fields(self, name: str) -> None:
        required = [f for f in dataclasses.fields(_events(name)) if f.default is _M]
        assert len(required) == 7

    def test_position_default_is_the_string_nil(self) -> None:
        assert _events("OrderFilledEvent").__dataclass_fields__["position"].default == "NIL"

    def test_defaults_apply_on_construction(self) -> None:
        fill = _events("OrderFilledEvent")(
            1.0,
            "o",
            _PAIR,
            TradeType.BUY,
            OrderType.LIMIT,
            Decimal(1),
            Decimal(1),
            AddedToCostTradeFee(),
        )
        assert (fill.exchange_trade_id, fill.exchange_order_id) == ("", "")
        assert (fill.leverage, fill.position) == (1, "NIL")
        assert _events("OrderCancelledEvent")(1.0, "o").exchange_order_id is None
        failure = _events("MarketOrderFailureEvent")(1.0, "o", OrderType.LIMIT)
        assert (failure.error_message, failure.error_type) == (None, None)
        created = _events("BuyOrderCreatedEvent")(
            1.0, OrderType.LIMIT, _PAIR, Decimal(1), Decimal(2), "o", 1.0
        )
        assert created.exchange_order_id is None
        assert (created.leverage, created.position) == (1, "NIL")

    def test_event_ids_are_hummingbot_market_event_values(self) -> None:
        ids = importlib.import_module(_EVENTS_MOD).MARKET_EVENT_IDS
        assert ids == {
            "BuyOrderCompletedEvent": 102,
            "SellOrderCompletedEvent": 103,
            "OrderCancelledEvent": 106,
            "OrderFilledEvent": 107,
            "MarketOrderFailureEvent": 198,
            "BuyOrderCreatedEvent": 200,
            "SellOrderCreatedEvent": 201,
        }


# ---------------------------------------------------------------------------
# 9. kraken_startup_cleanup compatibility
# ---------------------------------------------------------------------------


class TestStartupCleanup:
    async def test_in_flight_orders_is_a_plain_dict_of_in_flight_orders(
        self, connector: Any, seen: Recorder
    ) -> None:
        client_id = await _place(connector, seen)
        orders = connector.in_flight_orders
        assert isinstance(orders, dict)
        assert set(orders) == {client_id}
        assert isinstance(orders[client_id], InFlightOrder)

    async def test_order_missing_on_exchange_is_reconciled(
        self, connector: Any, gateway: KrakenGateway, rest: FakeKrakenRest, seen: Recorder
    ) -> None:
        await _place(connector, seen)
        rest.open_orders = {}
        assert await reconcile_stale_orders(gateway, connector) == 1

    async def test_order_still_open_on_exchange_is_untouched(
        self, connector: Any, gateway: KrakenGateway, rest: FakeKrakenRest, seen: Recorder
    ) -> None:
        client_id = await _place(connector, seen)
        rest.open_orders = {_TXID: _order_info(status="open")}
        assert await reconcile_stale_orders(gateway, connector) == 0
        assert connector.fetch_order(client_id).is_open is True


# ---------------------------------------------------------------------------
# 10. no hummingbot import
# ---------------------------------------------------------------------------


class TestNoHummingbotImport:
    @pytest.mark.parametrize("module_name", [_CORE_MOD, _EVENTS_MOD])
    def test_module_never_imports_hummingbot(self, module_name: str) -> None:
        source = Path(importlib.import_module(module_name).__file__ or "").read_text()
        imported: list[str] = []
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        assert not [m for m in imported if m == "hummingbot" or m.startswith("hummingbot.")]
