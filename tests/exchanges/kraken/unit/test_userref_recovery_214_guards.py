"""Guards for userref recovery (#214): an order adopted by the resolver is never failed."""

from __future__ import annotations

from typing import Any

from tests.exchanges.kraken.unit import test_userref_recovery_214 as rec

FAILURE = rec.FAILURE
UserrefRest, Recorder = rec.UserrefRest, rec.Recorder

# Fixtures reused as-is (pytest discovers them by module attribute).
bare_connector, bus, connector = rec.bare_connector, rec.bus, rec.connector
gateway, seen, rest = rec.gateway, rec.seen, rec.rest


class TestAdoptedOrderNotFailed:
    async def test_generic_addorder_error_after_adoption_keeps_order(
        self, connector: Any, rest: UserrefRest, seen: Recorder
    ) -> None:
        rest.add_order_gate.clear()  # AddOrder request stays live
        cid = rec._buy(connector)  # noqa: SLF001
        await rest.add_order_started.wait()
        userref = rest.userrefs[0]
        assert userref is not None
        txid = rest.register_on_exchange(userref, "open")

        await connector._resolve_unknown_orders()  # noqa: SLF001
        assert connector.fetch_order(cid).exchange_order_id == txid

        rest.add_order_exc = RuntimeError("boom: non-transport error")
        rest.add_order_gate.set()
        await connector.stop()  # drains the create task

        assert seen.of(FAILURE) == []
        assert cid in connector.in_flight_orders
        assert connector.fetch_order(cid).exchange_order_id == txid
        assert connector.txid_for_client_order_id(cid) == txid
