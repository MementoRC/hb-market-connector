"""Test doubles for interactive brokers — MockIb and related fakes."""

from __future__ import annotations

from unittest.mock import MagicMock


class MockIb:
    """Mock ib_async.IB instance supporting market data and historical data methods."""

    def __init__(self) -> None:
        self._depth_ticker = MagicMock()
        self._depth_ticker.domBids = []
        self._depth_ticker.domAsks = []
        self._cancel_mkt_depth_calls: list[object] = []
        self._historical_bars: list[object] = []

    def reqMktDepth(  # noqa: N802, N803
        self,
        contract: object,
        numRows: int = 10,  # noqa: N803
        isSmartDepth: bool = True,  # noqa: N803
    ) -> MagicMock:
        """Return a pre-configured FakeTicker with domBids/domAsks."""
        return self._depth_ticker

    def cancelMktDepth(self, contract: object, isSmartDepth: bool = True) -> None:  # noqa: N802, N803
        """Record that market depth cancellation was called."""
        self._cancel_mkt_depth_calls.append(contract)

    async def reqHistoricalDataAsync(self, contract: object, **kwargs: object) -> list[object]:  # noqa: N802
        """Return pre-configured historical bars."""
        return list(self._historical_bars)
