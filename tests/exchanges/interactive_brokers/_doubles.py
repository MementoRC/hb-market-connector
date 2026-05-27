"""Test doubles for interactive brokers — MockIb and related fakes."""

from __future__ import annotations


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


class MockIb:
    """Mock ib_async.IB instance supporting market data and historical data methods."""

    def __init__(self) -> None:
        self._depth_ticker = FakeTicker()
        self._cancel_mkt_depth_calls: list[object] = []
        self._historical_bars: list[object] = []

    def reqMktDepth(  # noqa: N802, N803
        self,
        contract: object,
        numRows: int = 10,  # noqa: N803
        isSmartDepth: bool = True,  # noqa: N803
    ) -> FakeTicker:
        """Return a pre-configured FakeTicker with domBids/domAsks."""
        return self._depth_ticker

    def cancelMktDepth(self, contract: object, isSmartDepth: bool = True) -> None:  # noqa: N802, N803
        """Record that market depth cancellation was called."""
        self._cancel_mkt_depth_calls.append(contract)

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
        """Return a FakeTicker with tickByTickAllLastEvent support."""
        return FakeTicker()

    def cancelTickByTickData(self, contract: object, tickType: str) -> None:  # noqa: N802, N803
        """No-op for test."""
        pass


class FakeTicker:
    """Fake ib_async.Ticker with updateEvent and tickByTickAllLastEvent support."""

    def __init__(self) -> None:
        self.domBids: list[object] = []
        self.domAsks: list[object] = []
        self.updateEvent = FakeEvent()
        self.tickByTickAllLastEvent = FakeEvent()
