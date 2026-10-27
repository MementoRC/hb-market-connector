"""Test doubles for interactive brokers — MockIb and related fakes."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

_FILL_TIME = datetime(2026, 1, 2, 15, 30, tzinfo=timezone.utc)  # noqa: UP017


class FakeEvent:
    """Minimal ib_async-style event supporting += / -= / ()."""

    def __init__(self) -> None:
        self._handlers: list[object] = []

    def __iadd__(self, handler: object) -> FakeEvent:
        self._handlers.append(handler)
        return self

    def __isub__(self, handler: object) -> FakeEvent:
        if handler in self._handlers:
            self._handlers.remove(handler)
        return self

    def __call__(self, *args: object, **kwargs: object) -> None:
        for h in list(self._handlers):
            h(*args, **kwargs)  # type: ignore[operator]


class FakeClient:
    """Fake ib_async.Client: records setConnectOptions."""

    MaxClientVersion = 178  # noqa: N815

    def __init__(self, owner: MockIb) -> None:
        self._owner = owner
        self.server_version = 176
        self.connect_options: list[str] = []
        # ib_async Client.getReqId: increasing ints from a settable start.
        self.next_req_id = 100
        self.req_id_calls = 0

    def getReqId(self) -> int:  # noqa: N802
        self.req_id_calls += 1
        req_id = self.next_req_id
        self.next_req_id += 1
        return req_id

    def serverVersion(self) -> int:  # noqa: N802
        return self.server_version

    def setConnectOptions(self, opts: str) -> None:  # noqa: N802
        self._owner.call_order.append("setConnectOptions")
        self.connect_options.append(opts)


class FakeWrapper:
    """Minimal ib_async Wrapper: the request-id bookkeeping for live tickers."""

    def __init__(self) -> None:
        self.ticker2ReqId: dict[str, dict[FakeTicker, int]] = {  # noqa: N815
            "mktData": {},
            "mktDepth": {},
            "tickByTick": {},
        }
        self.reqId2Ticker: dict[int, FakeTicker] = {}  # noqa: N815


class MockIb:
    """Mock ib_async.IB instance supporting market data and historical data methods."""

    def __init__(self) -> None:
        self._depth_ticker = FakeTicker()
        self._cancel_mkt_depth_calls: list[object] = []
        self._tick_ticker = FakeTicker()
        self._cancel_tick_by_tick_calls: list[tuple[object, str]] = []
        self._historical_bars: list[object] = []
        self.market_data_type_calls: list[int] = []
        self.connect_calls: list[dict[str, object]] = []
        self.errorEvent = FakeEvent()  # noqa: N815
        self.disconnectedEvent = FakeEvent()  # noqa: N815
        self.execDetailsEvent = FakeEvent()  # noqa: N815
        self.commissionReportEvent = FakeEvent()  # noqa: N815
        # reqExecutionsAsync: filters received, fills returned, optional error to raise.
        self.req_executions_filters: list[object] = []
        self.executions_result: list[object] = []
        self.executions_error: Exception | None = None
        self.client = FakeClient(self)
        # whatIfOrderAsync: (contract, order) calls, result served, optional error, hang switch.
        self.what_if_calls: list[tuple[object, object]] = []
        self.what_if_result: object = make_order_state()
        self.what_if_error: Exception | None = None
        self.what_if_hang: bool = False
        # Account state served by managedAccounts()/accountValues()/positions().
        self.managed_accounts: list[str] = []
        self.account_value_rows: list[object] = []
        self.position_rows: list[object] = []
        self.account_values_calls: list[str] = []
        self.positions_calls: list[str] = []
        self.connected = False
        self.disconnect_calls = 0
        # Number of upcoming connectAsync calls that raise before one succeeds.
        self.connect_failures = 0
        self.req_mkt_depth_calls: list[object] = []
        self.req_tick_by_tick_calls: list[object] = []
        # Ordered log of "setConnectOptions" / "connectAsync" calls.
        self.call_order: list[str] = []
        # L1 (reqMktData) tickers and the isSmartDepth flag of each depth request/cancel.
        self._l1_ticker = FakeTicker()
        self.req_mkt_data_calls: list[object] = []
        self.cancel_mkt_data_calls: list[object] = []
        self.req_mkt_depth_smart: list[bool] = []
        self.cancel_mkt_depth_smart: list[bool] = []
        # Ordered "reqMktData"/"cancelMktData"/"cancelMktDepth"/"disconnect" markers.
        self.event_log: list[str] = []
        # ib_async-style request ids: the wrapper maps each live ticker to its reqId.
        self.wrapper = FakeWrapper()
        self._next_req_id = 1001
        # Request ids in send order, for injecting IB errors against a specific request.
        self._depth_req_ids: list[int] = []
        self._mkt_data_req_ids: list[int] = []
        self._tick_req_ids: list[int] = []
        # placeOrder/cancelOrder recordings; trades are real ib_async Trade objects.
        self.placed_orders: list[Any] = []
        self.placed_contracts: list[Any] = []
        self.trades_by_order_id: dict[int, Any] = {}
        self.cancel_order_calls: list[Any] = []
        # orderIds whose cancelOrder raises, mapped to the exception.
        self.cancel_errors: dict[int, Exception] = {}
        # PnL subscriptions (ib_async 2.1 semantics): real PnL/PnLSingle objects filled and
        # emitted asynchronously after the request unless ``pnl_hang`` is set.
        self.pnlEvent = FakeEvent()  # noqa: N815
        self.pnlSingleEvent = FakeEvent()  # noqa: N815
        self.pnl_hang: bool = False
        self.pnl_values: dict[str, float] = {
            "dailyPnL": 12.34,
            "unrealizedPnL": -5.5,
            "realizedPnL": 0.0,
        }
        self.pnl_single_values: dict[str, float] = {
            "dailyPnL": 1.5,
            "unrealizedPnL": 20.25,
            "realizedPnL": float("nan"),
            "position": 100,
            "value": 15000.0,
        }
        self.pnl_req_calls: list[tuple[str, str]] = []
        self.pnl_cancel_calls: list[tuple[str, str]] = []
        self.pnl_single_req_calls: list[tuple[str, str, int]] = []
        self.pnl_single_cancel_calls: list[tuple[str, str, int]] = []
        self._pnl_keys: set[tuple[str, str]] = set()
        self._pnl_single_keys: set[tuple[str, str, int]] = set()
        # Live PnL/PnLSingle object returned by the latest request per key (ib_async mutates
        # and re-emits the same object on every update).
        self._pnl_live: dict[tuple[str, str], Any] = {}
        self._pnl_single_live: dict[tuple[str, str, int], Any] = {}

    def _reset_pnl_keys(self) -> None:
        """Mirror ib_async Wrapper.reset(), which clears pnlKey2ReqId/pnlSingleKey2ReqId.

        Real ib_async calls wrapper.reset() from Wrapper.connectionClosed() (socket drop)
        and IB.disconnect(), so after a drop the same key may be requested again without
        tripping the duplicate-key assert in IB.reqPnL / IB.reqPnLSingle.
        """
        self._pnl_keys.clear()
        self._pnl_single_keys.clear()

    def emit_pnl(self, account: str = "DU1111111", **values: float) -> None:
        """Push an update for the active reqPnL key: mutate the live PnL and fire pnlEvent."""
        pnl = self._pnl_live[(account, "")]
        for name, value in values.items():
            setattr(pnl, name, value)
        self.pnlEvent(pnl)

    def emit_pnl_single(self, con_id: int, account: str = "DU1111111", **values: float) -> None:
        """Push an update for the active reqPnLSingle key (mutate live object, fire event)."""
        pnl = self._pnl_single_live[(account, "", con_id)]
        for name, value in values.items():
            setattr(pnl, name, value)
        self.pnlSingleEvent(pnl)

    def reqPnL(self, account: str, modelCode: str = "") -> Any:  # noqa: N802, N803
        """Like ib_async.IB.reqPnL: asserts on a duplicate key, emits pnlEvent later."""
        from ib_async import PnL  # noqa: PLC0415

        key = (account, modelCode)
        assert key not in self._pnl_keys
        self._pnl_keys.add(key)
        self.pnl_req_calls.append(key)
        pnl = PnL(account, modelCode)
        self._pnl_live[key] = pnl
        if not self.pnl_hang:

            def _emit() -> None:
                for name, value in self.pnl_values.items():
                    setattr(pnl, name, value)
                self.pnlEvent(pnl)

            asyncio.get_running_loop().call_soon(_emit)
        return pnl

    def cancelPnL(self, account: str, modelCode: str = "") -> None:  # noqa: N802, N803
        self.pnl_cancel_calls.append((account, modelCode))
        self._pnl_keys.discard((account, modelCode))

    def reqPnLSingle(self, account: str, modelCode: str, conId: int) -> Any:  # noqa: N802, N803
        """Like ib_async.IB.reqPnLSingle: asserts on a duplicate key, emits later."""
        from ib_async import PnLSingle  # noqa: PLC0415

        key = (account, modelCode, conId)
        assert key not in self._pnl_single_keys
        self._pnl_single_keys.add(key)
        self.pnl_single_req_calls.append(key)
        pnl = PnLSingle(account, modelCode, conId)
        self._pnl_single_live[key] = pnl
        if not self.pnl_hang:

            def _emit() -> None:
                for name, value in self.pnl_single_values.items():
                    setattr(pnl, name, value)
                self.pnlSingleEvent(pnl)

            asyncio.get_running_loop().call_soon(_emit)
        return pnl

    def cancelPnLSingle(self, account: str, modelCode: str, conId: int) -> None:  # noqa: N802, N803
        self.pnl_single_cancel_calls.append((account, modelCode, conId))
        self._pnl_single_keys.discard((account, modelCode, conId))

    def emit_other_pnl(self, account: str = "DU9999999", **values: float) -> None:
        """Fire pnlEvent for an unrelated account."""
        from ib_async import PnL  # noqa: PLC0415

        pnl = PnL(account, "")
        for name, value in {"dailyPnL": 999.0, "unrealizedPnL": 999.0, **values}.items():
            setattr(pnl, name, value)
        self.pnlEvent(pnl)

    def emit_other_pnl_single(self, con_id: int = 999999, account: str = "") -> None:
        """Fire pnlSingleEvent for an unrelated conId."""
        from ib_async import PnLSingle  # noqa: PLC0415

        pnl = PnLSingle(account, "", con_id)
        pnl.position = 7
        pnl.dailyPnL = 999.0
        self.pnlSingleEvent(pnl)

    def placeOrder(self, contract: Any, order: Any) -> Any:  # noqa: N802
        """Record the order and return a real PendingSubmit ib_async Trade."""
        from ib_async import OrderStatus, Trade  # noqa: PLC0415

        if not order.orderId:
            order.orderId = self.client.getReqId()
        if self.connect_calls:
            # Like ib_async.IB.placeOrder: stamp the connected API client id.
            order.clientId = self.connect_calls[-1]["clientId"]
        self.placed_orders.append(order)
        self.placed_contracts.append(contract)
        trade = Trade(
            contract=contract,
            order=order,
            orderStatus=OrderStatus(orderId=order.orderId, status="PendingSubmit"),
        )
        self.trades_by_order_id[order.orderId] = trade
        return trade

    def cancelOrder(self, order: Any) -> None:  # noqa: N802
        """Record the cancel; raise the configured error for this orderId, if any."""
        self.cancel_order_calls.append(order)
        if order.orderId in self.cancel_errors:
            raise self.cancel_errors[order.orderId]

    def emit_status(self, order_id: int, status: str, *, perm_id: int | None = None) -> None:
        """Set the trade's status and fire its statusEvent, like an IB order update."""
        trade = self.trades_by_order_id[order_id]
        trade.orderStatus.status = status
        if perm_id is not None:
            trade.order.permId = perm_id
        trade.statusEvent.emit(trade)

    def emit_error(self, order_id: int, code: int = 201, msg: str = "order rejected") -> None:
        """Fire IB errorEvent for *order_id* (reqId == orderId), as the real wire does."""
        self.errorEvent(order_id, code, msg, None)

    def _start_ticker(self, req_type: str, ticker: FakeTicker) -> int:
        """Allocate the next reqId for *ticker* and record it as ib_async's wrapper does."""
        req_id = self._next_req_id
        self._next_req_id += 1
        self.wrapper.ticker2ReqId[req_type][ticker] = req_id
        self.wrapper.reqId2Ticker[req_id] = ticker
        return req_id

    async def connectAsync(self, **kwargs: object) -> None:  # noqa: N802
        """Record connection parameters; fail while ``connect_failures`` > 0."""
        self.call_order.append("connectAsync")
        self.connect_calls.append(kwargs)
        if self.connect_failures > 0:
            self.connect_failures -= 1
            raise ConnectionRefusedError("simulated connect failure")
        self.connected = True

    def isConnected(self) -> bool:  # noqa: N802
        return self.connected

    def openTrades(self) -> list:  # noqa: N802
        """Synchronous open-trades cache, like ib_async.IB.openTrades (always empty here)."""
        return []

    def managedAccounts(self) -> list[str]:  # noqa: N802
        """Synchronous, like ib_async.IB.managedAccounts (empty unless a test sets it)."""
        return list(self.managed_accounts)

    def accountValues(self, account: str = "") -> list[object]:  # noqa: N802
        """Synchronous account-value cache, like ib_async.IB.accountValues."""
        self.account_values_calls.append(account)
        return list(self.account_value_rows)

    def positions(self, account: str = "") -> list[object]:
        """Synchronous position cache, like ib_async.IB.positions."""
        self.positions_calls.append(account)
        return list(self.position_rows)

    def disconnect(self) -> None:
        """Like ib_async.IB.disconnect: closes the socket and fires disconnectedEvent."""
        self.disconnect_calls += 1
        self.event_log.append("disconnect")
        was_connected = self.connected
        self.connected = False
        self._reset_pnl_keys()
        if was_connected:
            self.disconnectedEvent()

    def simulate_drop(self) -> None:
        """Simulate the socket dropping underneath us (not via disconnect())."""
        self.connected = False
        self._reset_pnl_keys()
        self.disconnectedEvent()

    def rotate_tickers(self) -> None:
        """Make the next reqMktDepth/reqTickByTickData/reqMktData return brand-new tickers."""
        self._depth_ticker = FakeTicker()
        self._tick_ticker = FakeTicker()
        self._l1_ticker = FakeTicker()

    def reqMarketDataType(self, marketDataType: int) -> None:  # noqa: N802, N803
        """Synchronous, like ib_async.IB.reqMarketDataType."""
        self.market_data_type_calls.append(marketDataType)

    def reqMktDepth(  # noqa: N802, N803
        self,
        contract: object,
        numRows: int = 10,  # noqa: N803
        isSmartDepth: bool = True,  # noqa: N803
    ) -> FakeTicker:
        """Return a pre-configured FakeTicker with domBids/domAsks."""
        self.req_mkt_depth_calls.append(contract)
        self.req_mkt_depth_smart.append(isSmartDepth)
        self._depth_req_ids.append(self._start_ticker("mktDepth", self._depth_ticker))
        return self._depth_ticker

    def cancelMktDepth(self, contract: object, isSmartDepth: bool = True) -> None:  # noqa: N802, N803
        """Record that market depth cancellation was called."""
        self._cancel_mkt_depth_calls.append(contract)
        self.cancel_mkt_depth_smart.append(isSmartDepth)
        self.event_log.append("cancelMktDepth")

    def reqMktData(  # noqa: N802
        self,
        contract: object,
        genericTickList: str = "",  # noqa: N803
        snapshot: bool = False,
        regulatorySnapshot: bool = False,  # noqa: N803
        mktDataOptions: object = None,  # noqa: N803
    ) -> FakeTicker:
        """Return the pre-configured L1 FakeTicker (bid/ask/updateEvent)."""
        self.req_mkt_data_calls.append(contract)
        self.event_log.append("reqMktData")
        self._mkt_data_req_ids.append(self._start_ticker("mktData", self._l1_ticker))
        return self._l1_ticker

    def cancelMktData(self, contract: object) -> None:  # noqa: N802
        """Record that L1 market data cancellation was called."""
        self.cancel_mkt_data_calls.append(contract)
        self.event_log.append("cancelMktData")

    async def reqExecutionsAsync(self, execFilter: object = None) -> list[object]:  # noqa: N802, N803
        """Record the filter; return ``executions_result`` or raise ``executions_error``."""
        self.req_executions_filters.append(execFilter)
        if self.executions_error is not None:
            raise self.executions_error
        return list(self.executions_result)

    async def whatIfOrderAsync(self, contract: object, order: object) -> object:  # noqa: N802
        """Record the call; raise ``what_if_error``, hang, or return ``what_if_result``."""
        self.what_if_calls.append((contract, order))
        if self.what_if_error is not None:
            raise self.what_if_error
        if self.what_if_hang:
            await asyncio.Event().wait()
        return self.what_if_result

    async def reqHistoricalDataAsync(self, contract: object, **kwargs: object) -> list[object]:  # noqa: N802
        """Return pre-configured historical bars."""
        return list(self._historical_bars)

    def reqTickByTickData(  # noqa: N802
        self,
        contract: object,
        tickType: str,  # noqa: N803
        ignoreSize: int,  # noqa: N803
        useRTH: bool,  # noqa: N803
    ) -> FakeTicker:
        """Return the pre-configured tick-by-tick FakeTicker."""
        self.req_tick_by_tick_calls.append(contract)
        self._tick_req_ids.append(self._start_ticker("tickByTick", self._tick_ticker))
        return self._tick_ticker

    def cancelTickByTickData(self, contract: object, tickType: str) -> None:  # noqa: N802, N803
        """Record that tick-by-tick cancellation was called."""
        self._cancel_tick_by_tick_calls.append((contract, tickType))


def make_order_state(**kw: Any) -> Any:
    """Real ib_async.OrderState with sensible what-if margin strings (override via kwargs)."""
    try:
        from ib_async import OrderState  # noqa: PLC0415
    except ImportError:  # optional extra not installed
        OrderState = lambda **kw: SimpleNamespace(**kw)  # noqa: E731, N806

    fields: dict[str, Any] = {
        "initMarginBefore": "1000.00",
        "initMarginChange": "250.50",
        "initMarginAfter": "1250.50",
        "maintMarginBefore": "800.00",
        "maintMarginChange": "200.25",
        "maintMarginAfter": "1000.25",
        "equityWithLoanBefore": "50000.00",
        "equityWithLoanChange": "-100.00",
        "equityWithLoanAfter": "49900.00",
        "commission": 1.25,
        "minCommission": 1.0,
        "maxCommission": 2.5,
        "commissionCurrency": "USD",
        "warningText": "",
    }
    fields.update(kw)
    return OrderState(**fields)


def make_execution(  # noqa: PLR0913
    exec_id: str = "0001f4e8.6700a1b2.01.01",
    *,
    order_id: int = 11,
    side: str = "BOT",
    shares: float = 10.0,
    price: float = 101.25,
    cum_qty: float = 10.0,
) -> SimpleNamespace:
    """Fake ib_async.Execution."""
    return SimpleNamespace(
        execId=exec_id,
        orderId=order_id,
        permId=987654,
        clientId=1,
        side=side,
        shares=shares,
        price=price,
        cumQty=cum_qty,
        avgPrice=price,
        time=_FILL_TIME,
        acctNumber="DU123",
    )


def make_commission_report(
    exec_id: str = "0001f4e8.6700a1b2.01.01",
    commission: float = 1.05,
    currency: str = "USD",
) -> SimpleNamespace:
    """Fake ib_async.CommissionReport."""
    return SimpleNamespace(
        execId=exec_id, commission=commission, currency=currency, realizedPNL=0.0
    )


def make_fill(
    exec_id: str = "0001f4e8.6700a1b2.01.01",
    *,
    report: SimpleNamespace | None = None,
    **execution_kwargs: Any,
) -> SimpleNamespace:
    """Fake ib_async.Fill; ``report`` defaults to an unpopulated (empty execId) one."""
    return SimpleNamespace(
        contract=SimpleNamespace(conId=265598, symbol="AAPL"),
        execution=make_execution(exec_id, **execution_kwargs),
        commissionReport=report if report is not None else make_commission_report(""),
        time=_FILL_TIME,
    )


class FakeTicker:
    """Fake ib_async.Ticker: updateEvent, tickByTicks batch list, and L1 bid/ask (NaN = unset)."""

    def __init__(self) -> None:
        self.bid: float = float("nan")
        self.ask: float = float("nan")
        self.domBids: list[object] = []
        self.domAsks: list[object] = []
        # Raw per-update depth data (ib_async MktDepthData: position/operation/side/price/size).
        # ib_async replaces this with a fresh list for each network batch before updateEvent.
        self.domTicks: list[object] = []
        self.tickByTicks: list[object] = []
        self.updateEvent = FakeEvent()
