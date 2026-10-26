"""Wiring tests: IbGatewayTransport / get_candles / error router use IbPacer (#108 slice c)."""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_connector.exchanges.interactive_brokers.mixins.market_data import MarketDataMixin
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.transport import IbGatewayTransport


@pytest.fixture(autouse=True)
def _patch_ib_async_module():
    with patch.dict(sys.modules, {"ib_async": MagicMock()}):
        yield


class SpyPacer:
    """Records every pacing call and whether the guarded IB call ran inside the context."""

    def __init__(self, events: list[str] | None = None) -> None:
        self.events: list[str] = events if events is not None else []
        self.historical_calls: list[dict[str, Any]] = []
        self.violations: list[float | None] = []
        self.inside = False

    @asynccontextmanager
    async def historical(self, **kwargs: Any):
        self.historical_calls.append(kwargs)
        self.events.append("historical:enter")
        self.inside = True
        try:
            yield
        finally:
            self.inside = False
            self.events.append("historical:exit")

    async def matching_symbols(self) -> None:
        self.events.append("matching_symbols")

    def report_pacing_violation(self, retry_after: float | None = None) -> None:
        self.violations.append(retry_after)


def _spec() -> IbConnectionSpec:
    return IbConnectionSpec(host="127.0.0.1", port=4002, client_id=1, paper=True)


def _make_transport(pacer: Any = None, *, with_kwarg: bool = True) -> IbGatewayTransport:
    ib = MagicMock()
    ib.errorEvent = MagicMock()
    ib.errorEvent.__iadd__ = MagicMock()
    ib.errorEvent.__isub__ = MagicMock()
    with patch("market_connector.exchanges.interactive_brokers.transport.IB", return_value=ib):
        if with_kwarg:
            return IbGatewayTransport(_spec(), pacer=pacer)
        return IbGatewayTransport(_spec())


class TestTransportPacer:
    def test_default_pacer_is_ib_pacer(self) -> None:
        from market_connector.exchanges.interactive_brokers.pacing import IbPacer

        transport = _make_transport(with_kwarg=False)
        assert isinstance(transport.pacer, IbPacer)

    def test_injected_pacer_is_stored(self) -> None:
        pacer = SpyPacer()
        assert _make_transport(pacer).pacer is pacer

    def test_pacer_none_builds_a_fresh_default(self) -> None:
        from market_connector.exchanges.interactive_brokers.pacing import IbPacer

        assert isinstance(_make_transport(None).pacer, IbPacer)

    def test_transports_do_not_share_default_pacers(self) -> None:
        assert (
            _make_transport(with_kwarg=False).pacer is not _make_transport(with_kwarg=False).pacer
        )


class TestMatchSymbolsPacing:
    async def test_pacer_awaited_before_request(self) -> None:
        pacer = SpyPacer()
        transport = _make_transport(pacer)

        async def req(pattern: str) -> list[object]:
            pacer.events.append("reqMatchingSymbolsAsync")
            return []

        transport._ib.reqMatchingSymbolsAsync = req

        await transport.match_symbols("AAPL")

        assert pacer.events == ["matching_symbols", "reqMatchingSymbolsAsync"]

    async def test_request_not_sent_if_pacer_raises(self) -> None:
        pacer = SpyPacer()
        pacer.matching_symbols = AsyncMock(side_effect=RuntimeError("blocked"))  # type: ignore[method-assign]
        transport = _make_transport(pacer)
        transport._ib.reqMatchingSymbolsAsync = AsyncMock(return_value=[])

        with pytest.raises(RuntimeError, match="blocked"):
            await transport.match_symbols("AAPL")

        transport._ib.reqMatchingSymbolsAsync.assert_not_called()


class _Transport:
    """Transport double for the mixin: spy pacer plus a recording reqHistoricalDataAsync."""

    def __init__(self, pacer: SpyPacer) -> None:
        self.pacer = pacer
        self._ib = MagicMock()
        self.sent: list[dict[str, Any]] = []
        self.ran_inside: list[bool] = []

        async def req(contract: Any, **kwargs: Any) -> list[object]:
            self.ran_inside.append(pacer.inside)
            self.sent.append({"contract": contract, **kwargs})
            return []

        self._ib.reqHistoricalDataAsync = req


class _Resolver:
    def __init__(self) -> None:
        self.contracts: dict[str, Any] = {}

    async def resolve_from_pair(self, pair: str) -> Any:
        return self.contracts.setdefault(
            pair,
            SimpleNamespace(conId=100 + len(self.contracts), exchange="SMART", symbol=pair),
        )


class _Host(MarketDataMixin):
    def __init__(self, transport: _Transport) -> None:
        self._transport = transport
        self._contract_resolver = _Resolver()
        self._ready = True

    async def ensure_ready(self) -> None:
        pass


def _host() -> tuple[_Host, SpyPacer, _Transport]:
    pacer = SpyPacer()
    transport = _Transport(pacer)
    return _Host(transport), pacer, transport


class TestGetCandlesPacing:
    async def test_request_runs_inside_pacer_context(self) -> None:
        host, pacer, transport = _host()

        await host.get_candles("AAPL-USD", interval="5m", limit=10)

        assert len(pacer.historical_calls) == 1
        assert transport.ran_inside == [True]
        assert pacer.events == ["historical:enter", "historical:exit"]

    async def test_context_exits_when_request_raises(self) -> None:
        host, pacer, transport = _host()
        transport._ib.reqHistoricalDataAsync = AsyncMock(side_effect=RuntimeError("ib down"))

        with pytest.raises(RuntimeError, match="ib down"):
            await host.get_candles("AAPL-USD", interval="5m", limit=10)

        assert pacer.events == ["historical:enter", "historical:exit"]

    async def test_pacer_fields_come_from_the_sent_request(self) -> None:
        host, pacer, transport = _host()

        await host.get_candles("AAPL-USD", interval="5m", limit=10)

        call = pacer.historical_calls[0]
        sent = transport.sent[0]
        assert call["bar_size"] == sent["barSizeSetting"] == "5 mins"
        assert call["what_to_show"] == sent["whatToShow"] == "TRADES"
        assert call["exchange"] == sent["contract"].exchange == "SMART"
        assert "contract_key" in call
        assert "request_key" in call

    async def test_identical_calls_share_request_key(self) -> None:
        host, pacer, _ = _host()

        await host.get_candles("AAPL-USD", interval="5m", limit=10)
        await host.get_candles("AAPL-USD", interval="5m", limit=10)

        first, second = pacer.historical_calls
        assert first["request_key"] == second["request_key"]
        assert first["contract_key"] == second["contract_key"]

    async def test_different_contract_changes_request_key(self) -> None:
        host, pacer, _ = _host()

        await host.get_candles("AAPL-USD", interval="5m", limit=10)
        await host.get_candles("MSFT-USD", interval="5m", limit=10)

        first, second = pacer.historical_calls
        assert first["request_key"] != second["request_key"]
        assert first["contract_key"] != second["contract_key"]

    async def test_different_duration_changes_request_key(self) -> None:
        host, pacer, _ = _host()

        await host.get_candles("AAPL-USD", interval="5m", limit=10)
        await host.get_candles("AAPL-USD", interval="5m", limit=20)

        first, second = pacer.historical_calls
        assert first["request_key"] != second["request_key"]
        assert first["contract_key"] == second["contract_key"]

    async def test_different_bar_size_changes_request_key(self) -> None:
        host, pacer, _ = _host()

        await host.get_candles("AAPL-USD", interval="5m", limit=10)
        await host.get_candles("AAPL-USD", interval="15m", limit=10)

        first, second = pacer.historical_calls
        assert first["request_key"] != second["request_key"]

    async def test_request_key_covers_end_time_what_to_show_and_use_rth(self) -> None:
        """End time, whatToShow and useRTH are fixed by get_candles today; the key must see them.

        Pins the contract via the transport request actually sent: if any of those fields in the
        sent request were to change, the recorded key must change with it.  Today they are
        constant, so two identical calls yield equal keys and the sent fields are the fixed trio.
        """
        host, pacer, transport = _host()

        await host.get_candles("AAPL-USD", interval="1d", limit=5)
        await host.get_candles("AAPL-USD", interval="1d", limit=5)

        assert {(s["endDateTime"], s["whatToShow"], s["useRTH"]) for s in transport.sent} == {
            ("", "TRADES", False)
        }
        assert pacer.historical_calls[0]["request_key"] == pacer.historical_calls[1]["request_key"]


class TestErrorRouterPacingCallback:
    @staticmethod
    def _router_with_spy():
        from market_connector.exchanges.interactive_brokers._error_router import _ErrorRouter

        calls: list[float | None] = []
        return _ErrorRouter(market_data_type=None, on_pacing_violation=calls.append), calls

    @staticmethod
    def _fire(router: Any, code: int, msg: str) -> None:
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        router._pending_request_waiters[1] = fut
        router.on_error(req_id=1, code=code, msg=msg)
        if fut.done() and not fut.cancelled():
            fut.exception()  # mark retrieved

    async def test_162_pacing_violation_invokes_callback(self) -> None:
        from market_connector.exchanges.interactive_brokers._error_router import (
            HISTORICAL_PACING_RETRY_AFTER,
        )

        router, calls = self._router_with_spy()
        self._fire(router, 162, "Historical Market Data Service error message: pacing violation")
        assert calls == [HISTORICAL_PACING_RETRY_AFTER]

    async def test_420_invokes_callback_with_existing_retry_value(self) -> None:
        from market_connector.exchanges.interactive_brokers._error_router import (
            MESSAGE_RATE_RETRY_AFTER,
        )

        router, calls = self._router_with_spy()
        self._fire(router, 420, "Max rate of messages per second has been exceeded")
        assert calls == [MESSAGE_RATE_RETRY_AFTER]

    async def test_100_does_not_invoke_callback(self) -> None:
        router, calls = self._router_with_spy()
        self._fire(router, 100, "Max rate of messages per second has been exceeded")
        assert calls == []

    async def test_162_no_market_data_permissions_does_not_invoke_callback(self) -> None:
        router, calls = self._router_with_spy()
        self._fire(router, 162, "Historical Market Data Service error: No market data permissions")
        assert calls == []

    async def test_162_other_message_does_not_invoke_callback(self) -> None:
        router, calls = self._router_with_spy()
        self._fire(router, 162, "Historical Market Data Service error message: HMDS query returned")
        assert calls == []

    async def test_exception_mapping_unchanged_with_callback(self) -> None:
        from market_connector.exchanges.interactive_brokers._error_router import (
            HISTORICAL_PACING_RETRY_AFTER,
        )
        from market_connector.exchanges.interactive_brokers.exceptions import RateLimitError

        router, _ = self._router_with_spy()
        fut = asyncio.get_running_loop().create_future()
        router._pending_request_waiters[1] = fut
        router.on_error(req_id=1, code=162, msg="pacing violation")
        exc = fut.exception()
        assert isinstance(exc, RateLimitError)
        assert exc.retry_after == HISTORICAL_PACING_RETRY_AFTER

    async def test_callback_is_optional(self) -> None:
        from market_connector.exchanges.interactive_brokers._error_router import _ErrorRouter

        router = _ErrorRouter(market_data_type=None, on_pacing_violation=None)
        self._fire(router, 162, "pacing violation")

    async def test_transport_routes_pacing_violation_to_its_pacer(self) -> None:
        pacer = SpyPacer()
        transport = _make_transport(pacer)

        self._fire(transport._error_router, 162, "pacing violation")

        assert len(pacer.violations) == 1
        assert pacer.violations[0] is not None
