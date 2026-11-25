"""RED regression tests for PR #331 review (Interactive Brokers): teardown of the fill-bus tap.

``gateway.stop()`` must detach the leg-fill hub from the transport bus; ``start()`` must
re-attach it exactly once. Runs without ib_async (``MockIb`` + in-repo fake fills).
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING
from unittest.mock import patch

from ._doubles import make_fill
from .test_leg_fill_replay_323 import TAG_A, Sink, _started

if TYPE_CHECKING:
    from market_connector.exchanges.interactive_brokers.transport import IbGatewayTransport

_D = Decimal


def _publish(transport: IbGatewayTransport, exec_id: str, cum: float) -> None:
    """Publish one execution of ``TAG_A`` on the transport's fill bus."""
    transport._ingest_fill(
        make_fill(exec_id, order_id=11, cum_qty=cum, shares=cum, order_ref=TAG_A),
        reconciled=False,
    )


async def test_fill_published_after_stop_is_not_ingested() -> None:
    gateway, transport, _ = await _started()
    sink = Sink()
    gateway.subscribe_leg_fills(sink.on_fill)
    hub = gateway.__dict__["_leg_fill_hub"]

    await gateway.stop()
    with patch.object(hub.ledger, "record", wraps=hub.ledger.record) as record:
        _publish(transport, "e1.01", 4)

    assert record.call_count == 0
    assert list(hub.ledger.latest()) == []
    assert sink.fills == []


async def test_restart_reattaches_exactly_once() -> None:
    gateway, transport, _ = await _started()
    sink = Sink()
    gateway.subscribe_leg_fills(sink.on_fill)
    hub = gateway.__dict__["_leg_fill_hub"]

    await gateway.stop()
    await gateway.start()
    with patch.object(hub.ledger, "record", wraps=hub.ledger.record) as record:
        _publish(transport, "e1.01", 4)

    assert record.call_count == 1
    assert [f.cumulative_filled for f in hub.ledger.latest()] == [_D(4)]
    assert sink.cumulative == [_D(4)]
