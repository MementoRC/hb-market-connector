"""Compatibility checks against the real ib_async package (issue #50).

The other IB unit tests replace ``ib_async`` with doubles, so they cannot
notice when the library's API drifts from what the connector calls. These
tests import the real package (installed in the ``ci`` and
``interactive-brokers`` pixi environments) and check the API surface used by
the transport, order handle, contract resolver and market-data mixins.
"""

from __future__ import annotations

import dataclasses
import inspect

import pytest

ib_async = pytest.importorskip("ib_async")


def _field_names(cls: type) -> set[str]:
    if hasattr(cls, "_fields"):  # NamedTuple
        return set(cls._fields)
    return {f.name for f in dataclasses.fields(cls)}


def test_client_exposes_server_version_surface() -> None:
    """Issue #59: the transport reads Client.serverVersion() and MaxClientVersion."""
    client_cls = ib_async.client.Client
    assert callable(client_cls.serverVersion)
    assert isinstance(client_cls.MaxClientVersion, int)


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("connectAsync", {"host", "port", "clientId"}),
        ("disconnect", set()),
        ("isConnected", set()),
        ("placeOrder", {"contract", "order"}),
        ("cancelOrder", {"order"}),
        ("openTrades", set()),
        ("reqContractDetailsAsync", {"contract"}),
        (
            "reqHistoricalDataAsync",
            {"contract", "endDateTime", "durationStr", "barSizeSetting", "whatToShow", "useRTH"},
        ),
        ("reqMktDepth", {"contract", "numRows", "isSmartDepth"}),
        ("cancelMktDepth", {"contract", "isSmartDepth"}),
        ("reqTickByTickData", {"contract", "tickType", "numberOfTicks", "ignoreSize"}),
        ("cancelTickByTickData", {"contract", "tickType"}),
        ("reqPnL", {"account", "modelCode"}),
        ("cancelPnL", {"account", "modelCode"}),
        ("reqPnLSingle", {"account", "modelCode", "conId"}),
        ("cancelPnLSingle", {"account", "modelCode", "conId"}),
    ],
)
def test_ib_method_accepts_params(method: str, params: set[str]) -> None:
    func = getattr(ib_async.IB, method)
    assert params <= set(inspect.signature(func).parameters)


@pytest.mark.parametrize(
    ("cls_name", "fields"),
    [
        ("Order", {"action", "orderType", "totalQuantity", "lmtPrice", "orderId", "permId"}),
        ("OrderStatus", {"status", "filled", "avgFillPrice"}),
        ("Contract", {"symbol", "secType", "currency", "exchange", "conId", "primaryExchange"}),
        ("ContractDetails", {"contract", "tradingHours"}),
        ("BarData", {"date", "open", "high", "low", "close", "volume"}),
        ("DOMLevel", {"price", "size"}),
        ("TickByTickAllLast", {"time", "price", "size"}),
        ("PnL", {"account", "modelCode", "dailyPnL", "unrealizedPnL", "realizedPnL"}),
        (
            "PnLSingle",
            {
                "account",
                "modelCode",
                "conId",
                "dailyPnL",
                "unrealizedPnL",
                "realizedPnL",
                "position",
                "value",
            },
        ),
    ],
)
def test_object_fields(cls_name: str, fields: set[str]) -> None:
    assert fields <= _field_names(getattr(ib_async, cls_name))


async def test_ib_exposes_error_event() -> None:
    assert hasattr(ib_async.IB(), "errorEvent")


async def test_ib_exposes_pnl_events() -> None:
    ib = ib_async.IB()
    assert hasattr(ib, "pnlEvent")
    assert hasattr(ib, "pnlSingleEvent")


async def test_trade_exposes_status_event_and_order() -> None:
    trade = ib_async.Trade()
    assert hasattr(trade, "statusEvent")
    assert hasattr(trade, "order")
    assert hasattr(trade, "orderStatus")


async def test_ticker_exposes_depth_and_update_event() -> None:
    ticker = ib_async.Ticker()
    assert hasattr(ticker, "updateEvent")
    assert hasattr(ticker, "domBids")
    assert hasattr(ticker, "domAsks")
    assert hasattr(ticker, "tickByTicks")


async def test_ticker_delivers_tick_by_tick_via_update_event() -> None:
    ticker = ib_async.Ticker()
    assert isinstance(ticker.tickByTicks, list)
    assert hasattr(ticker, "updateEvent")
    assert not hasattr(ticker, "tickByTickAllLastEvent")
