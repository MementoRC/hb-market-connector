"""RED regression test for PR #331 review (Coinbase): tag reuse after a late subscriber's replay.

A ``client_order_id`` reused by a new order (new exchange id) restarts at a low cumulative. A
subscriber replayed the old order's total must still receive the new order's fills.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from market_connector.exchanges.coinbase.mixins.leg_fills import track_leg_placement
from tests.exchanges.coinbase.unit import test_leg_fills_249 as base

if TYPE_CHECKING:
    from market_connector.exchanges.coinbase.coinbase_gateway import CoinbaseGateway

gateway = base.gateway
rest = base.rest

_D = Decimal
_A = base._A


async def test_late_subscriber_receives_fills_of_a_reused_tag(
    gateway: CoinbaseGateway,  # noqa: F811
    rest: base._FakeRest,  # noqa: F811
) -> None:
    xid = await base._place(gateway, _A)
    base._script(rest, xid, base._row("0.6", "FILLED"))
    sub_a, sub_b = base.Sink(), base.Sink()
    unsub_a = gateway.subscribe_leg_fills(sub_a.on_fill)
    assert await base._wait_until(lambda: sub_a.cumulative == [_D("0.6")])
    await base._quiet()

    unsub_b = gateway.subscribe_leg_fills(sub_b.on_fill)  # late: replayed 0.6
    try:
        assert await base._wait_until(lambda: sub_b.cumulative == [_D("0.6")])

        rest.scripts["X-new"] = [base._row("0.1")]
        track_leg_placement(gateway, _A, "X-new")  # same tag, new exchange id
        assert await base._wait_until(lambda: sub_a.cumulative[-1:] == [_D("0.1")])
        await base._quiet()

        assert sub_a.cumulative == [_D("0.6"), _D("0.1")]
        assert sub_b.cumulative == [_D("0.6"), _D("0.1")]
    finally:
        unsub_a()
        unsub_b()
