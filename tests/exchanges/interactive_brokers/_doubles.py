"""Test doubles for interactive brokers — MockIb and related fakes."""

from __future__ import annotations

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

    def __init__(self, owner: MockIb) -> None:
        self._owner = owner
        self.connect_options: list[str] = []

    def setConnectOptions(self, opts: str) -> None:  # noqa: N802
        self._owner.call_order.append("setConnectOptions")
        self.connect_options.append(opts)


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
        self.connected = False
        self.disconnect_calls = 0
        # Number of upcoming connectAsync calls that raise before one succeeds.
        self.connect_failures = 0
        self.req_mkt_depth_calls: list[object] = []
        self.req_tick_by_tick_calls: list[object] = []
        # Ordered log of "setConnectOptions" / "connectAsync" calls.
        self.call_order: list[str] = []

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

    def disconnect(self) -> None:
        """Like ib_async.IB.disconnect: closes the socket and fires disconnectedEvent."""
        self.disconnect_calls += 1
        was_connected = self.connected
        self.connected = False
        if was_connected:
            self.disconnectedEvent()

    def simulate_drop(self) -> None:
        """Simulate the socket dropping underneath us (not via disconnect())."""
        self.connected = False
        self.disconnectedEvent()

    def rotate_tickers(self) -> None:
        """Make the next reqMktDepth/reqTickByTickData return brand-new tickers."""
        self._depth_ticker = FakeTicker()
        self._tick_ticker = FakeTicker()

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
        return self._depth_ticker

    def cancelMktDepth(self, contract: object, isSmartDepth: bool = True) -> None:  # noqa: N802, N803
        """Record that market depth cancellation was called."""
        self._cancel_mkt_depth_calls.append(contract)

    async def reqExecutionsAsync(self, execFilter: object = None) -> list[object]:  # noqa: N802, N803
        """Record the filter; return ``executions_result`` or raise ``executions_error``."""
        self.req_executions_filters.append(execFilter)
        if self.executions_error is not None:
            raise self.executions_error
        return list(self.executions_result)

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
        return self._tick_ticker

    def cancelTickByTickData(self, contract: object, tickType: str) -> None:  # noqa: N802, N803
        """Record that tick-by-tick cancellation was called."""
        self._cancel_tick_by_tick_calls.append((contract, tickType))


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
    """Fake ib_async.Ticker: updateEvent plus a tickByTicks batch list (as in ib_async)."""

    def __init__(self) -> None:
        self.domBids: list[object] = []
        self.domAsks: list[object] = []
        self.tickByTicks: list[object] = []
        self.updateEvent = FakeEvent()
