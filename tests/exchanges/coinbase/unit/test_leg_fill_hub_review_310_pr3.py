"""Regression tests for the #310 PR 3 review (sourcery) of the Coinbase LegFill hub gate.

* Bounded ``known``/``last`` drop terminal orders first (Kraken keeps terminal records apart).
* Early buffer: age-based expiry on an injectable clock, oldest-first, non-link ids ignored.
* An update without a client order id resolves it from the registration (or at track time).
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from market_connector.exchanges.coinbase.mixins import leg_fills
from market_connector.exchanges.coinbase.mixins.leg_fills import _hub
from tests.exchanges.coinbase.unit import test_leg_fill_hub_gate_310_pr3 as base

if TYPE_CHECKING:
    import pytest

    from market_connector.exchanges.coinbase.coinbase_gateway import CoinbaseGateway

gateway = base.gateway
rest = base.rest
sink = base.sink

_D = Decimal
_A, _B = base._A, base._B
_C = base._CODEC.encode(7, 2)
_xid = base._xid


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class TestBoundedKnown:
    async def test_terminal_orders_are_evicted_before_live_ones(
        self,
        gateway: CoinbaseGateway,  # noqa: F811
        rest: base._FakeRest,  # noqa: F811
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(leg_fills, "_MAX_LAST", 2)
        xa = await base._place(gateway, _A)  # oldest, but terminal below
        xb = await base._place(gateway, _B)  # live
        hub = _hub(gateway)
        hub.observe(_A, "1", "100", xa)
        rest.scripts[xa] = [base._row("1", "FILLED")]
        await base._poll_once(gateway, _A)
        hub.observe(_B, "1", "100", xb)

        xc = await base._place(gateway, _C)

        assert xa not in hub.known
        assert xa not in hub.last
        assert xb in hub.known
        assert xc in hub.known
        assert hub.last[xb] == _D("1")  # the live order keeps its cumulative

    async def test_oldest_is_evicted_when_nothing_is_terminal(
        self,
        gateway: CoinbaseGateway,  # noqa: F811
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(leg_fills, "_MAX_LAST", 2)
        xa = await base._place(gateway, _A)
        xb = await base._place(gateway, _B)
        _hub(gateway).observe(_A, "1", "100", xa)
        xc = await base._place(gateway, _C)

        hub = _hub(gateway)
        assert list(hub.known) == [xb, xc]
        assert xa not in hub.last


class TestEarlyBufferExpiry:
    async def test_stale_unrelated_observations_expire(
        self,
        gateway: CoinbaseGateway,  # noqa: F811
        sink: base.Sink,  # noqa: F811
    ) -> None:
        hub = _hub(gateway)
        clock = _Clock()
        hub.clock = clock
        hub.observe(_B, "1", "100", _xid(_B))  # never placed here
        clock.now = leg_fills._EARLY_TTL + 1
        hub.observe(_A, "0.5", "100", _xid(_A))  # real order, placement not registered yet

        assert list(hub.early) == [_xid(_A)]

        await base._place(gateway, _A)
        assert await base._wait_until(lambda: sink.cumulative(_A) == [_D("0.5")])

    async def test_observation_older_than_ttl_is_not_replayed(
        self,
        gateway: CoinbaseGateway,  # noqa: F811
        sink: base.Sink,  # noqa: F811
    ) -> None:
        hub = _hub(gateway)
        clock = _Clock()
        hub.clock = clock
        hub.observe(_A, "0.5", "100", _xid(_A))
        clock.now = leg_fills._EARLY_TTL + 1

        await base._place(gateway, _A)
        await base._settle()

        assert sink.fills == []

    async def test_crowding_evicts_oldest_first_not_the_newest(
        self,
        gateway: CoinbaseGateway,  # noqa: F811
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(leg_fills, "_MAX_EARLY_ORDERS", 2)
        hub = _hub(gateway)
        for cid in (_A, _B, _C):
            hub.observe(cid, "1", "100", _xid(cid))

        assert list(hub.early) == [_xid(_B), _xid(_C)]
        assert set(hub.early_at) == set(hub.early)

    async def test_non_link_client_ids_are_never_buffered(
        self,
        gateway: CoinbaseGateway,  # noqa: F811
    ) -> None:
        hub = _hub(gateway)
        hub.observe("manual-order-1", "1", "100", "X-manual")

        assert not hub.early
        assert not hub.early_at


class TestMissingClientOrderId:
    async def test_registered_order_resolves_cid_from_exchange_id(
        self,
        gateway: CoinbaseGateway,  # noqa: F811
        sink: base.Sink,  # noqa: F811
    ) -> None:
        xid = await base._place(gateway, _A)
        _hub(gateway).observe("", "1", "100", xid)

        assert [f.client_order_id for f in sink.fills] == [_A]
        assert sink.cumulative() == [_D("1")]

    async def test_unregistered_order_resolves_cid_at_track_time(
        self,
        gateway: CoinbaseGateway,  # noqa: F811
        sink: base.Sink,  # noqa: F811
    ) -> None:
        hub = _hub(gateway)
        hub.observe("", "1", "100", _xid(_A))
        await base._settle()
        assert sink.fills == []

        await base._place(gateway, _A)

        assert await base._wait_until(lambda: len(sink.fills) == 1)
        assert sink.fills[0].client_order_id == _A
        assert sink.fills[0].cumulative_filled == _D("1")
