"""RED regression tests for PR #331 review (Kraken): tag reuse after a late subscriber's replay.

A leg tag reused by a new order instance (new txid) starts afresh at a low cumulative. A
subscriber that was replayed the old instance's total must still receive the new instance's
fills exactly as an earlier subscriber does; its replay floor must not swallow them.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from tests.exchanges.kraken.unit import test_leg_fill_replay_323 as r323

if TYPE_CHECKING:
    from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway

gateway = r323.gateway
bus = r323.bus
seen = r323.seen
rest = r323.rest
core = r323.core

_D = Decimal


async def test_late_subscriber_receives_fills_of_a_reused_tag(
    gateway: KrakenGateway,  # noqa: F811
    core: Any,  # noqa: F811
) -> None:
    first_txid = await r323._place(gateway, r323.TAG_A)
    sub_a, sub_b = r323.Sink(), r323.Sink()
    gateway.subscribe_leg_fills(sub_a.on_fill)
    r323._trade(core, first_txid, "T1", "0.5", "27000", seq=1)
    await r323._settle()

    gateway.subscribe_leg_fills(sub_b.on_fill)  # late: replayed 0.5
    await r323._settle()
    assert sub_b.cumulative == [_D("0.5")]

    second_txid = await r323._place(gateway, r323.TAG_A)  # same tag, new order instance
    assert second_txid != first_txid
    r323._trade(core, second_txid, "T2", "0.1", "27010", seq=2)
    await r323._settle()

    assert sub_a.cumulative == [_D("0.5"), _D("0.1")]
    assert sub_b.cumulative == [_D("0.5"), _D("0.1")]
