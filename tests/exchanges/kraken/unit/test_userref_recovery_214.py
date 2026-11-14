"""Recovering a Kraken order whose AddOrder response was lost, via ``userref`` (#214 item 4), RED.

Contract pinned here (none of it exists yet):

A. Gateway (``OrdersMixin``)
   * ``place_order(..., *, userref: int | None = None)`` sends ``userref=<int>`` in the
     AddOrder form body; no ``userref`` field when ``None``.
   * ``get_open_orders(userref=None)`` sends ``userref`` in the OpenOrders body when given.
   * ``get_closed_orders(userref=None)`` hits ``/0/private/ClosedOrders`` (REST endpoint
     name ``closed_orders``), sends ``userref`` when given, and returns ``{txid: OrderInfo}``
     unwrapped from ``result.closed``.

B. Core (``KrakenConnectorCore``)
   1. Every submitted order carries a unique positive int32 ``userref``.
   2. ``httpx.TimeoutException`` / ``httpx.TransportError`` from AddOrder does NOT fail the
      order: no failure event, still in ``in_flight_orders``, state ``UNKNOWN``.
   3. ``await core._resolve_unknown_orders()`` queries open then closed orders by userref;
      found -> adopt the txid and emit the normal Created event; absent from both -> FAILED
      with exactly one ``MarketOrderFailureEvent``.
   4. ``_poll_once`` resolves PENDING_CREATE / UNKNOWN orders that have no txid the same way.
   5. A non-transport AddOrder error (``EOrder:...``) still fails immediately.

Only the REST transport is faked; the order-state enum value ``UNKNOWN`` is referred to by
name so the tests fail (not error at import) until it exists.
"""

from __future__ import annotations

import asyncio
import inspect
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway
from market_connector.primitives import OrderType, TradeType
from market_connector.transport.response import Response
from tests.exchanges.kraken.unit import test_kraken_connector_core_211 as base
from tests.exchanges.kraken.unit import test_private_request_wire_215 as wire

BUY_CREATED, FAILURE = base.BUY_CREATED, base.FAILURE
_AMOUNT, _PAIR, _PRICE = base._AMOUNT, base._PAIR, base._PRICE  # noqa: SLF001
_order_info = base._order_info  # noqa: SLF001
FakeKrakenRest, Recorder = base.FakeKrakenRest, base.Recorder

# Fixtures reused as-is (pytest discovers them by module attribute); ``rest`` is overridden.
bare_connector, bus, connector = base.bare_connector, base.bus, base.connector
gateway, seen = base.gateway, base.seen

_INT32_MAX = 2**31 - 1
_LOST = [httpx.ReadTimeout("response lost"), httpx.ConnectError("connection reset")]


# ---------------------------------------------------------------------------
# A. Gateway wire format
# ---------------------------------------------------------------------------

_OPEN_REPLY = {"error": [], "result": {"open": {}}}
_CLOSED_REPLY = {
    "error": [],
    "result": {
        "closed": {"OCLOSED-1": {**_order_info(status="closed", vol_exec="0.5"), "userref": 77}},
        "count": 1,
    },
}


def _fields(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.content.decode("utf-8"), keep_blank_values=True)


class TestGatewayUserref:
    async def test_place_order_sends_userref_in_the_form_body(self) -> None:
        gw, sent = wire._kraken(wire._KRAKEN_ADD_ORDER_REPLY)  # noqa: SLF001

        await gw.place_order(
            "XBTUSD",
            OrderType.LIMIT,
            TradeType.BUY,
            Decimal("0.01"),
            Decimal("100.5"),
            userref=123456,
        )

        assert sent[0].url.path == "/0/private/AddOrder"
        assert _fields(sent[0])["userref"] == ["123456"]

    async def test_place_order_without_userref_sends_no_userref_field(self) -> None:
        gw, sent = wire._kraken(wire._KRAKEN_ADD_ORDER_REPLY)  # noqa: SLF001

        await gw.place_order(
            "XBTUSD", OrderType.LIMIT, TradeType.BUY, Decimal("0.01"), Decimal("100.5")
        )

        assert "userref" not in _fields(sent[0])

    def test_place_order_userref_is_keyword_only_and_defaults_to_none(self) -> None:
        params = inspect.signature(KrakenGateway.place_order).parameters

        assert "userref" in params
        assert params["userref"].kind is inspect.Parameter.KEYWORD_ONLY
        assert params["userref"].default is None

    async def test_get_open_orders_sends_userref_when_given(self) -> None:
        gw, sent = wire._kraken(_OPEN_REPLY)  # noqa: SLF001

        await gw.get_open_orders(userref=42)

        assert sent[0].url.path == "/0/private/OpenOrders"
        assert _fields(sent[0])["userref"] == ["42"]

    async def test_get_open_orders_without_userref_sends_none(self) -> None:
        gw, sent = wire._kraken(_OPEN_REPLY)  # noqa: SLF001

        await gw.get_open_orders()

        assert "userref" not in _fields(sent[0])

    async def test_get_closed_orders_sends_userref_to_closed_orders_endpoint(self) -> None:
        gw, sent = wire._kraken(_CLOSED_REPLY)  # noqa: SLF001

        result = await gw.get_closed_orders(userref=77)

        assert sent[0].method == "POST"
        assert sent[0].url.path == "/0/private/ClosedOrders"
        assert _fields(sent[0])["userref"] == ["77"]
        assert set(result) == {"OCLOSED-1"}

    async def test_get_closed_orders_without_userref_sends_none(self) -> None:
        gw, sent = wire._kraken(_CLOSED_REPLY)  # noqa: SLF001

        await gw.get_closed_orders()

        assert sent[0].url.path == "/0/private/ClosedOrders"
        assert "userref" not in _fields(sent[0])


# ---------------------------------------------------------------------------
# Fake REST for the core: userref-aware, can lose the AddOrder response
# ---------------------------------------------------------------------------


class UserrefRest(FakeKrakenRest):
    """FakeKrakenRest that records userrefs, can hold/lose AddOrder, and filters by userref.

    ``lost_as`` ("open" | "closed" | None): where a lost-response order ended up on the
    exchange (None: the request never arrived).
    """

    def __init__(self) -> None:
        super().__init__()
        self.add_order_exc: Exception | None = None
        self.lost_as: str | None = None
        self.add_order_gate = asyncio.Event()
        self.add_order_gate.set()
        self.add_order_started = asyncio.Event()
        self.userrefs: list[int | None] = []
        self.closed_orders: dict[str, dict[str, Any]] = {}

    def register_on_exchange(self, userref: int, where: str) -> str:
        txid = f"OLOST-{userref}"
        status = "open" if where == "open" else "closed"
        info = {**_order_info(status=status, vol_exec="0" if where == "open" else "0.5")}
        info["userref"] = userref
        (self.open_orders if where == "open" else self.closed_orders)[txid] = info
        return txid

    async def request(self, endpoint_name: str, *args: Any, **kwargs: Any) -> Any:
        data = kwargs.get("data") or kwargs.get("params") or {}
        if endpoint_name == "add_order":
            raw = data.get("userref")
            userref = None if raw is None else int(raw)
            self.userrefs.append(userref)
            self.add_order_started.set()
            await self.add_order_gate.wait()
            if self.add_order_exc is not None:
                self.calls.append((endpoint_name, dict(data)))
                if self.lost_as is not None and userref is not None:
                    self.register_on_exchange(userref, self.lost_as)
                raise self.add_order_exc
        elif endpoint_name in {"open_orders", "closed_orders"}:
            self.calls.append((endpoint_name, dict(data)))
            return self._userref_reply(endpoint_name, data.get("userref"))
        return await super().request(endpoint_name, *args, **kwargs)

    def _userref_reply(self, endpoint: str, userref: Any) -> Response:
        source = self.open_orders if endpoint == "open_orders" else self.closed_orders
        key = "open" if endpoint == "open_orders" else "closed"
        picked = {
            txid: info
            for txid, info in source.items()
            if userref is None or info.get("userref") == int(userref)
        }
        result: dict[str, Any] = {key: picked}
        if key == "closed":
            result["count"] = len(picked)
        return Response(
            raw={"error": [], "result": result},
            status_code=200,
            headers=httpx.Headers(),
            _endpoint=endpoint,
        )

    def lookups(self) -> list[tuple[str, Any]]:
        """``(endpoint, userref)`` for every open/closed lookup, in call order."""
        return [
            (name, body.get("userref"))
            for name, body in self.calls
            if name in {"open_orders", "closed_orders"}
        ]


@pytest.fixture
def rest() -> UserrefRest:
    return UserrefRest()


def _buy(core: Any) -> str:
    return core.buy(_PAIR, _AMOUNT, base.OrderType.LIMIT, _PRICE)


async def _lose_one(core: Any, rest: UserrefRest, exc: Exception, lost_as: str | None) -> str:
    """Place one order whose AddOrder response is lost; drain the create task."""
    rest.add_order_exc = exc
    rest.lost_as = lost_as
    cid = _buy(core)
    await core.stop()  # drains the background create task
    return cid


def _state_name(core: Any, cid: str) -> str:
    return core.fetch_order(cid).current_state.name


# ---------------------------------------------------------------------------
# B1. Every order carries a unique int32 userref
# ---------------------------------------------------------------------------


class TestB1UserrefAssigned:
    async def test_each_order_sends_a_unique_positive_int32_userref(
        self, connector: Any, rest: UserrefRest, seen: Recorder
    ) -> None:
        for _ in range(5):
            _buy(connector)
        await seen.wait(BUY_CREATED, 5)

        assert len(rest.userrefs) == 5
        assert all(u is not None for u in rest.userrefs), "AddOrder sent without userref"
        refs = [u for u in rest.userrefs if u is not None]
        assert all(1 <= u <= _INT32_MAX for u in refs)
        assert len(set(refs)) == 5


# ---------------------------------------------------------------------------
# B2. A lost AddOrder response leaves the order UNKNOWN, not failed
# ---------------------------------------------------------------------------


class TestB2LostResponseIsUnknown:
    @pytest.mark.parametrize("exc", _LOST, ids=["timeout", "transport"])
    async def test_order_stays_tracked_as_unknown(
        self, connector: Any, rest: UserrefRest, seen: Recorder, exc: Exception
    ) -> None:
        cid = await _lose_one(connector, rest, exc, lost_as=None)

        assert seen.of(FAILURE) == []
        assert cid in connector.in_flight_orders
        assert _state_name(connector, cid) == "UNKNOWN"
        assert connector.fetch_order(cid).exchange_order_id is None


# ---------------------------------------------------------------------------
# B3. _resolve_unknown_orders: open by userref, then closed by userref
# ---------------------------------------------------------------------------


class TestB3Resolution:
    async def test_found_in_open_orders_is_adopted(
        self, connector: Any, rest: UserrefRest, seen: Recorder
    ) -> None:
        cid = await _lose_one(connector, rest, _LOST[0], lost_as="open")
        userref = rest.userrefs[0]

        await connector._resolve_unknown_orders()  # noqa: SLF001

        txid = f"OLOST-{userref}"
        assert connector.fetch_order(cid).exchange_order_id == txid
        assert connector.txid_for_client_order_id(cid) == txid
        assert connector.client_order_id_for_txid(txid) == cid
        assert cid in connector.in_flight_orders
        assert seen.of(FAILURE) == []
        assert [e.exchange_order_id for e in seen.of(BUY_CREATED)] == [txid]
        assert [(n, str(r)) for n, r in rest.lookups()] == [("open_orders", str(userref))]

    async def test_found_only_in_closed_orders_is_adopted(
        self, connector: Any, rest: UserrefRest, seen: Recorder
    ) -> None:
        cid = await _lose_one(connector, rest, _LOST[1], lost_as="closed")
        userref = rest.userrefs[0]

        await connector._resolve_unknown_orders()  # noqa: SLF001

        txid = f"OLOST-{userref}"
        assert connector.fetch_order(cid).exchange_order_id == txid
        assert seen.of(FAILURE) == []
        assert [e.exchange_order_id for e in seen.of(BUY_CREATED)] == [txid]
        names = [name for name, _ in rest.lookups()]
        assert names == ["open_orders", "closed_orders"]  # open first, then closed
        assert {str(ref) for _, ref in rest.lookups()} == {str(userref)}

    async def test_absent_from_both_fails_exactly_once(
        self, connector: Any, rest: UserrefRest, seen: Recorder
    ) -> None:
        cid = await _lose_one(connector, rest, _LOST[0], lost_as=None)
        userref = rest.userrefs[0]

        await connector._resolve_unknown_orders()  # noqa: SLF001
        await connector._resolve_unknown_orders()  # noqa: SLF001  # idempotent

        assert [f.order_id for f in seen.of(FAILURE)] == [cid]
        assert cid not in connector.in_flight_orders
        assert _state_name(connector, cid) == "FAILED"
        assert seen.of(BUY_CREATED) == []
        names = [name for name, _ in rest.lookups()]
        assert names[:2] == ["open_orders", "closed_orders"]
        assert {str(ref) for _, ref in rest.lookups()} == {str(userref)}


# ---------------------------------------------------------------------------
# B4. The poller resolves txid-less PENDING_CREATE / UNKNOWN orders
# ---------------------------------------------------------------------------


class TestB4PollerResolves:
    async def test_poll_adopts_an_unknown_order(
        self, connector: Any, rest: UserrefRest, seen: Recorder
    ) -> None:
        cid = await _lose_one(connector, rest, _LOST[0], lost_as="open")

        await connector._poll_once()  # noqa: SLF001

        txid = f"OLOST-{rest.userrefs[0]}"
        assert connector.fetch_order(cid).exchange_order_id == txid
        assert [e.exchange_order_id for e in seen.of(BUY_CREATED)] == [txid]
        assert seen.of(FAILURE) == []

    async def test_poll_fails_an_unknown_order_absent_from_the_exchange(
        self, connector: Any, rest: UserrefRest, seen: Recorder
    ) -> None:
        cid = await _lose_one(connector, rest, _LOST[1], lost_as=None)
        assert cid in connector.in_flight_orders  # not failed by the lost response itself
        assert seen.of(FAILURE) == []

        await connector._poll_once()  # noqa: SLF001
        await connector._poll_once()  # noqa: SLF001

        assert [f.order_id for f in seen.of(FAILURE)] == [cid]
        assert cid not in connector.in_flight_orders

    async def test_poll_resolves_a_pending_create_without_txid(
        self, connector: Any, rest: UserrefRest, seen: Recorder
    ) -> None:
        rest.add_order_gate.clear()  # AddOrder reply is held: order stays PENDING_CREATE
        cid = _buy(connector)
        await rest.add_order_started.wait()
        userref = rest.userrefs[0]
        assert userref is not None, "AddOrder sent without userref"
        txid = rest.register_on_exchange(userref, "open")
        assert connector.fetch_order(cid).exchange_order_id is None

        try:
            await connector._poll_once()  # noqa: SLF001

            assert connector.fetch_order(cid).exchange_order_id == txid
            assert [e.exchange_order_id for e in seen.of(BUY_CREATED)] == [txid]
            assert seen.of(FAILURE) == []
        finally:
            rest.add_order_exc = _LOST[0]
            rest.add_order_gate.set()
            await connector.stop()


# ---------------------------------------------------------------------------
# B5. Regression guard: a definite rejection still fails immediately
# ---------------------------------------------------------------------------


class TestB5RejectionStillFails:
    async def test_eorder_error_reply_fails_at_once(
        self, connector: Any, rest: UserrefRest, seen: Recorder
    ) -> None:
        rest.add_order_errors = ["EOrder:Insufficient funds"]

        cid = _buy(connector)
        await seen.wait(FAILURE)

        assert [f.order_id for f in seen.of(FAILURE)] == [cid]
        assert cid not in connector.in_flight_orders
        assert seen.of(BUY_CREATED) == []
        assert rest.lookups() == []  # no recovery lookup for a definite rejection
