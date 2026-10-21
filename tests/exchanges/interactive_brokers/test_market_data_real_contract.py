"""Regression tests for hb-market-connector issue #72.

The transport keys its persistent L1 / depth streams by the contract it is handed. In
production that is a frozen ``ResolvedContract`` whose ``metadata`` is a dict, so hashing it
raised ``TypeError: unhashable type: 'dict'``. These tests use the REAL ``ResolvedContract``
(the existing persistent-market-data tests use a hashable fake and missed it).
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

from market_connector.contracts.instrument import InstrumentRef, InstrumentType
from market_connector.contracts.protocols import ResolvedContract
from market_connector.exchanges.interactive_brokers.ib_gateway import IbGatewayGateway

from .test_persistent_market_data import (
    _EXPECTED_ASKS,
    _EXPECTED_BIDS,
    _IDLE_SHORT,
    _PAST_IDLE,
    Rig,
    _feed_levels,
    _feed_levels_soon,
    _rig,
    _set_l1,
)

try:
    from ib_async import Contract as _NativeContract
except ImportError:  # pragma: no cover
    _NativeContract = None


def _native(con_id: int) -> Any:
    if _NativeContract is not None:
        return _NativeContract(conId=con_id, symbol="AAPL", secType="STK", exchange="SMART")
    return type("Native", (), {"conId": con_id})()


def _resolved(con_id: int = 265598, symbol: str = "AAPL") -> ResolvedContract:
    return ResolvedContract(
        ref=InstrumentRef(
            symbol=symbol, instrument_type=InstrumentType.STOCK, quote_currency="USD"
        ),
        native=_native(con_id),
        contract_id=str(con_id),
        metadata={"primary_exchange": "NASDAQ", "trading_hours": None},
    )


class RealResolver:
    """Resolver returning a FRESH ResolvedContract per call (as after a cache rebuild)."""

    async def resolve_from_pair(self, pair: str) -> ResolvedContract:
        return _resolved(265598, pair.partition("-")[0])


def _real_rig(**spec_kwargs: Any) -> Rig:
    rig = _rig(**spec_kwargs)
    rig.gateway = IbGatewayGateway(
        transport=rig.transport, signer=MagicMock(), contract_resolver=RealResolver()
    )
    return rig


async def test_depth_snapshot_with_real_resolved_contract() -> None:
    rig = _real_rig()
    _feed_levels_soon(rig.ib._depth_ticker)
    bids, asks = await rig.transport.depth_snapshot(_resolved())
    assert (bids, asks) == (_EXPECTED_BIDS, _EXPECTED_ASKS)


async def test_get_orderbook_through_gateway_with_real_resolved_contract() -> None:
    rig = _real_rig()
    _feed_levels_soon(rig.ib._depth_ticker)
    snapshot = await rig.gateway.get_orderbook("AAPL-USD")
    assert snapshot.bids == _EXPECTED_BIDS
    assert snapshot.asks == _EXPECTED_ASKS


async def test_top_of_book_with_real_resolved_contract() -> None:
    rig = _real_rig()
    _set_l1(rig.ib._l1_ticker, 150.0, 150.5)
    bid, ask = await rig.transport.top_of_book(_resolved())
    assert (bid, ask) == (Decimal("150.0"), Decimal("150.5"))


async def test_get_mid_price_through_gateway_with_real_resolved_contract() -> None:
    rig = _real_rig()
    _set_l1(rig.ib._l1_ticker, 150.0, 150.5)
    assert await rig.gateway.get_mid_price("AAPL-USD") == Decimal("150.25")


async def test_equal_resolved_contracts_share_one_depth_stream() -> None:
    rig = _real_rig(depth_idle_release=_IDLE_SHORT)
    first, second = _resolved(), _resolved()
    assert first is not second
    _feed_levels_soon(rig.ib._depth_ticker)
    await rig.transport.depth_snapshot(first)
    await rig.transport.depth_snapshot(second)
    assert len(rig.ib.req_mkt_depth_calls) == 1
    assert len(rig.transport._depth_streams) == 1


async def test_shared_depth_stream_releases_once_after_idle() -> None:
    rig = _real_rig(depth_idle_release=_IDLE_SHORT)
    _feed_levels_soon(rig.ib._depth_ticker)
    await rig.transport.depth_snapshot(_resolved())
    await rig.transport.depth_snapshot(_resolved())
    assert rig.ib._cancel_mkt_depth_calls == []
    await asyncio.sleep(_PAST_IDLE)
    assert len(rig.ib._cancel_mkt_depth_calls) == 1
    assert rig.transport._depth_streams == {}


async def test_subscribe_and_snapshot_with_equal_contracts_share_stream() -> None:
    rig = _real_rig()
    handle = rig.transport.subscribe("depth", _resolved(), lambda t: None)
    _feed_levels(rig.ib._depth_ticker)
    bids, _asks = await rig.transport.depth_snapshot(_resolved())
    assert bids == _EXPECTED_BIDS
    assert len(rig.ib.req_mkt_depth_calls) == 1
    handle.close()


async def test_equal_resolved_contracts_share_one_l1_ticker() -> None:
    rig = _real_rig()
    _set_l1(rig.ib._l1_ticker, 150.0, 150.5)
    await rig.transport.top_of_book(_resolved())
    await rig.transport.top_of_book(_resolved())
    assert len(rig.ib.req_mkt_data_calls) == 1


async def test_different_con_ids_get_separate_depth_streams() -> None:
    rig = _real_rig()
    _feed_levels_soon(rig.ib._depth_ticker)
    await rig.transport.depth_snapshot(_resolved(265598, "AAPL"))
    # The new MSFT stream waits for its own first book update; feed one.
    _feed_levels_soon(rig.ib._depth_ticker)
    await rig.transport.depth_snapshot(_resolved(272093, "MSFT"))
    assert len(rig.ib.req_mkt_depth_calls) == 2
    assert len(rig.transport._depth_streams) == 2


async def test_different_con_ids_get_separate_l1_tickers() -> None:
    rig = _real_rig()
    _set_l1(rig.ib._l1_ticker, 150.0, 150.5)
    await rig.transport.top_of_book(_resolved(265598, "AAPL"))
    await rig.transport.top_of_book(_resolved(272093, "MSFT"))
    assert len(rig.ib.req_mkt_data_calls) == 2
