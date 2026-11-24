"""RED acceptance tests for #250 (Interactive Brokers): ``get_leg`` / ``list_open_legs``.

Contract pinned here (none of it exists yet):

* ``market_connector.exchanges.interactive_brokers.linked_order_codec.IbOrderRefLinkCodec`` is a
  ``PrefixedLinkIdCodec`` (tag ``mcl-<group>-<leg>``, unbounded group id) carried in
  ``Order.orderRef``.
* ``IbGatewayGateway`` satisfies ``SupportsLegLookup``.
* ``LegSnapshot.client_order_id == str(order.orderId)``. ``get_leg(client_order_id=...)`` matches
  only orders whose ``clientId`` is this connection's client id; ``get_leg(link_tag=...)``
  matches ``orderRef`` across every client.
* ``get_leg`` search order: ``openTrades()`` cache, then
  ``await reqCompletedOrdersAsync(apiOnly=True)``, then ``None``. Unknown id or no key -> ``None``.
  Disconnected -> raises ``ConnectionLostError`` (never ``None``).
* ``list_open_legs(target, group=None)``: open trades on the target's contract symbol whose
  ``orderRef`` decodes; ``group`` compares ``str(decoded group)``; completed orders never listed.
* State mapping from ``orderStatus.status`` (see ``_STATE_CASES``); unknown status -> UNKNOWN.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from data_type_primitives import OrderState

from market_connector.exchanges.interactive_brokers.exceptions import ConnectionLostError
from market_connector.exchanges.interactive_brokers.ib_gateway import IbGatewayGateway
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.transport import IbGatewayTransport
from market_connector.protocols import LegSnapshot, SupportsLegLookup

from ._doubles import FakeEvent

OWN = 5
OTHER = 9
SYMBOL = "AAPL"
_D = Decimal


def _codec() -> Any:
    from market_connector.exchanges.interactive_brokers.linked_order_codec import (  # noqa: PLC0415
        IbOrderRefLinkCodec,
    )

    return IbOrderRefLinkCodec()


def _tag(group: int, leg: int) -> str:
    return f"mcl-{group}-{leg}"


def _trade(
    order_id: int,
    *,
    client_id: int = OWN,
    ref: str | None = "",
    status: str = "Submitted",
    filled: float | None = 0.0,
    perm_id: int = 0,
    symbol: str = SYMBOL,
) -> SimpleNamespace:
    return SimpleNamespace(
        contract=SimpleNamespace(symbol=symbol, conId=1),
        order=SimpleNamespace(
            orderId=order_id, clientId=client_id, permId=perm_id, orderRef=ref, account="DU1"
        ),
        orderStatus=SimpleNamespace(status=status, filled=filled, avgFillPrice=0.0),
    )


def _build(
    open_trades: list[Any], completed: list[Any] | None = None, *, connected: bool = True
) -> tuple[IbGatewayGateway, MagicMock]:
    ib = MagicMock()
    ib.isConnected = MagicMock(return_value=connected)
    ib.openTrades = MagicMock(return_value=list(open_trades))
    ib.reqCompletedOrdersAsync = AsyncMock(return_value=list(completed or []))
    ib.errorEvent = MagicMock()
    ib.errorEvent.__iadd__ = MagicMock()
    ib.errorEvent.__isub__ = MagicMock()
    with patch("market_connector.exchanges.interactive_brokers.transport.IB", return_value=ib):
        transport = IbGatewayTransport(IbConnectionSpec(client_id=OWN))
    transport._error_router._is_connected = connected
    resolver = MagicMock()

    async def _resolve_from_pair(pair: str) -> Any:
        return SimpleNamespace(native=SimpleNamespace(symbol=pair, conId=1))

    resolver.resolve_from_pair = AsyncMock(side_effect=_resolve_from_pair)
    resolver.resolve = AsyncMock(
        return_value=SimpleNamespace(native=SimpleNamespace(symbol=SYMBOL, conId=1))
    )
    gateway = IbGatewayGateway(transport=transport, signer=MagicMock(), contract_resolver=resolver)
    return gateway, ib


class TestIbOrderRefLinkCodec:
    def test_is_a_prefixed_link_codec(self) -> None:
        from market_connector.exchanges._prefixed_link_codec import PrefixedLinkIdCodec  # noqa: PLC0415

        assert isinstance(_codec(), PrefixedLinkIdCodec)

    def test_encode_decode_round_trip(self) -> None:
        codec = _codec()

        assert codec.encode(5, 1) == "mcl-5-1"
        assert codec.decode("mcl-5-1") == (5, 1)

    def test_group_id_is_unbounded(self) -> None:
        codec = _codec()

        assert codec.decode(codec.encode(2**40, 0)) == (2**40, 0)

    @pytest.mark.parametrize("foreign", ["", "coinbase-1", "mcl-05-1", "mcl-1-16", "mcl-1", "x"])
    def test_foreign_tags_decode_to_none(self, foreign: str) -> None:
        assert _codec().decode(foreign) is None


def test_gateway_is_supports_leg_lookup() -> None:
    gateway, _ = _build([])

    assert isinstance(gateway, SupportsLegLookup)


class TestGetLegByClientOrderId:
    async def test_open_cache_hit_builds_full_snapshot(self) -> None:
        gateway, ib = _build(
            [_trade(11, ref=_tag(5, 1), status="Submitted", filled=0.4, perm_id=987)]
        )

        snap = await gateway.get_leg(SYMBOL, client_order_id="11")

        assert snap == LegSnapshot(
            client_order_id="11",
            link_tag=_tag(5, 1),
            state=OrderState.PARTIALLY_FILLED,
            filled=_D("0.4"),
            venue_order_id="987",
            group_id="5",
        )
        ib.reqCompletedOrdersAsync.assert_not_awaited()

    async def test_other_clients_order_with_same_id_does_not_match(self) -> None:
        gateway, _ = _build([_trade(11, client_id=OTHER)])

        assert await gateway.get_leg(SYMBOL, client_order_id="11") is None

    async def test_own_order_wins_over_other_clients_same_id(self) -> None:
        gateway, _ = _build(
            [_trade(11, client_id=OTHER, ref=_tag(1, 0)), _trade(11, ref=_tag(2, 0))]
        )

        snap = await gateway.get_leg(SYMBOL, client_order_id="11")

        assert snap is not None
        assert snap.group_id == "2"

    async def test_other_clients_completed_order_does_not_match(self) -> None:
        gateway, _ = _build([], [_trade(11, client_id=OTHER, status="Filled")])

        assert await gateway.get_leg(SYMBOL, client_order_id="11") is None

    async def test_foreign_ref_found_without_link_tag_or_group(self) -> None:
        gateway, _ = _build([_trade(12, ref="manual-note")])

        snap = await gateway.get_leg(SYMBOL, client_order_id="12")

        assert snap is not None
        assert (snap.client_order_id, snap.link_tag, snap.group_id) == ("12", None, None)

    async def test_missing_ref_found_without_link_tag_or_group(self) -> None:
        gateway, _ = _build([_trade(13, ref=None)])

        snap = await gateway.get_leg(SYMBOL, client_order_id="13")

        assert snap is not None
        assert (snap.link_tag, snap.group_id) == (None, None)


def _build_live(open_trades: list[Any], completed: list[Any]) -> tuple[IbGatewayGateway, MagicMock]:
    """Like ``_build`` but with real open-order/status events and handlers attached."""
    gateway, ib = _build(open_trades, completed)
    ib.openOrderEvent = FakeEvent()
    ib.orderStatusEvent = FakeEvent()
    gateway._transport._attach_handlers()
    return gateway, ib


def _completed_live(perm_id: int, *, status: str, filled: float) -> SimpleNamespace:
    """Live shape of a completed trade: orderId=0, clientId=0, no fills, empty orderRef."""
    return SimpleNamespace(
        contract=SimpleNamespace(symbol=SYMBOL, conId=1),
        order=SimpleNamespace(orderId=0, clientId=0, permId=perm_id, orderRef="", account="DU1"),
        orderStatus=SimpleNamespace(status=status, filled=filled, avgFillPrice=0.0),
        fills=[],
    )


class TestGetLegCompletedFallback:
    async def test_falls_back_to_completed_orders_api_only(self) -> None:
        gateway, ib = _build_live([_trade(1)], [_completed_live(55, status="Filled", filled=1.0)])
        ib.openOrderEvent.emit(_trade(20, ref=_tag(7, 0), perm_id=55))

        snap = await gateway.get_leg(SYMBOL, client_order_id="20")

        ib.reqCompletedOrdersAsync.assert_awaited_once_with(apiOnly=True)
        assert snap is not None
        assert (snap.state, snap.filled, snap.venue_order_id) == (
            OrderState.FILLED,
            _D(1),
            "55",
        )
        assert snap.client_order_id == "20"

    async def test_completed_cancelled_is_canceled(self) -> None:
        gateway, ib = _build_live([], [_completed_live(66, status="Cancelled", filled=0.1)])
        ib.orderStatusEvent.emit(_trade(21, perm_id=66))

        snap = await gateway.get_leg(SYMBOL, client_order_id="21")

        assert snap is not None
        assert (snap.state, snap.filled) == (OrderState.CANCELED, _D("0.1"))

    async def test_open_cache_is_searched_before_completed(self) -> None:
        gateway, ib = _build(
            [_trade(22, status="Submitted")], [_trade(22, status="Filled", filled=1.0)]
        )

        snap = await gateway.get_leg(SYMBOL, client_order_id="22")

        assert snap is not None
        assert snap.state is OrderState.OPEN
        ib.reqCompletedOrdersAsync.assert_not_awaited()

    async def test_unknown_id_returns_none_after_searching_both(self) -> None:
        gateway, ib = _build([_trade(1)], [_trade(2, status="Filled")])

        assert await gateway.get_leg(SYMBOL, client_order_id="999") is None
        ib.reqCompletedOrdersAsync.assert_awaited_once_with(apiOnly=True)

    async def test_non_numeric_client_order_id_returns_none_not_an_error(self) -> None:
        gateway, _ = _build([_trade(1)])

        assert await gateway.get_leg(SYMBOL, client_order_id="not-a-number") is None


class TestGetLegByLinkTag:
    async def test_finds_open_leg_by_order_ref(self) -> None:
        gateway, _ = _build([_trade(1), _trade(31, ref=_tag(5, 0), filled=0.0)])

        snap = await gateway.get_leg(SYMBOL, link_tag=_tag(5, 0))

        assert snap is not None
        assert (snap.client_order_id, snap.link_tag, snap.group_id) == ("31", _tag(5, 0), "5")

    async def test_matches_across_clients(self) -> None:
        gateway, _ = _build([_trade(32, client_id=OTHER, ref=_tag(8, 1))])

        snap = await gateway.get_leg(SYMBOL, link_tag=_tag(8, 1))

        assert snap is not None
        assert snap.client_order_id == "32"

    async def test_finds_completed_leg_by_order_ref(self) -> None:
        gateway, ib = _build([], [_trade(33, ref=_tag(9, 2), status="Filled", filled=2.0)])

        snap = await gateway.get_leg(SYMBOL, link_tag=_tag(9, 2))

        ib.reqCompletedOrdersAsync.assert_awaited_once_with(apiOnly=True)
        assert snap is not None
        assert (snap.client_order_id, snap.state) == ("33", OrderState.FILLED)

    async def test_unknown_link_tag_returns_none(self) -> None:
        gateway, _ = _build([_trade(1, ref=_tag(1, 0))], [_trade(2, ref=_tag(2, 0))])

        assert await gateway.get_leg(SYMBOL, link_tag=_tag(99, 0)) is None
        assert await gateway.get_leg(SYMBOL, link_tag="no-such-tag") is None


class TestGetLegArguments:
    async def test_neither_key_is_none_not_an_error(self) -> None:
        gateway, ib = _build([_trade(1)])

        assert await gateway.get_leg(SYMBOL) is None
        assert await gateway.get_leg(SYMBOL, client_order_id="", link_tag="") is None
        ib.reqCompletedOrdersAsync.assert_not_awaited()
        ib.reqCompletedOrdersAsync.assert_not_called()
        ib.openTrades.assert_not_called()
        ib.reqAllOpenOrdersAsync.assert_not_called()

    async def test_filled_is_decimal_not_float(self) -> None:
        gateway, _ = _build([_trade(1, filled=0.5)])

        snap = await gateway.get_leg(SYMBOL, client_order_id="1")

        assert snap is not None
        assert isinstance(snap.filled, Decimal)

    async def test_disconnected_by_client_order_id_raises(self) -> None:
        gateway, _ = _build([_trade(1)], connected=False)

        with pytest.raises(ConnectionLostError):
            await gateway.get_leg(SYMBOL, client_order_id="1")

    async def test_disconnected_by_link_tag_raises(self) -> None:
        gateway, _ = _build([_trade(1, ref=_tag(1, 0))], connected=False)

        with pytest.raises(ConnectionLostError):
            await gateway.get_leg(SYMBOL, link_tag=_tag(1, 0))


_STATE_CASES = [
    ("PendingSubmit", 0.0, OrderState.PENDING_CREATE),
    ("ApiPending", 0.0, OrderState.PENDING_CREATE),
    ("PreSubmitted", 0.0, OrderState.OPEN),
    ("Submitted", 0.0, OrderState.OPEN),
    ("PreSubmitted", 0.5, OrderState.PARTIALLY_FILLED),
    ("Submitted", 0.5, OrderState.PARTIALLY_FILLED),
    ("PendingCancel", 0.0, OrderState.PENDING_CANCEL),
    ("Filled", 1.0, OrderState.FILLED),
    ("Cancelled", 0.0, OrderState.CANCELED),
    ("ApiCancelled", 0.0, OrderState.CANCELED),
    ("Inactive", 0.0, OrderState.FAILED),
    ("SomethingNew", 0.0, OrderState.UNKNOWN),
]


class TestStateMapping:
    @pytest.mark.parametrize(("status", "filled", "expected"), _STATE_CASES)
    async def test_status_maps_to_order_state(
        self, status: str, filled: float, expected: OrderState
    ) -> None:
        gateway, _ = _build([_trade(1, status=status, filled=filled)])

        snap = await gateway.get_leg(SYMBOL, client_order_id="1")

        assert snap is not None
        assert snap.state is expected
        assert snap.filled == _D(filled)

    async def test_none_filled_is_zero(self) -> None:
        gateway, _ = _build([_trade(1, status="Submitted", filled=None)])

        snap = await gateway.get_leg(SYMBOL, client_order_id="1")

        assert snap is not None
        assert (snap.filled, snap.state) == (_D(0), OrderState.OPEN)

    async def test_perm_id_zero_means_no_venue_order_id(self) -> None:
        gateway, _ = _build([_trade(1, perm_id=0)])

        snap = await gateway.get_leg(SYMBOL, client_order_id="1")

        assert snap is not None
        assert snap.venue_order_id is None

    async def test_perm_id_nonzero_is_str_venue_order_id(self) -> None:
        gateway, _ = _build([_trade(1, perm_id=123456)])

        snap = await gateway.get_leg(SYMBOL, client_order_id="1")

        assert snap is not None
        assert snap.venue_order_id == "123456"


class TestListOpenLegs:
    @staticmethod
    def _fixture() -> list[Any]:
        return [
            _trade(1, ref=_tag(5, 0), filled=0.4, perm_id=11),
            _trade(2, ref=_tag(5, 1)),
            _trade(3, ref=_tag(6, 0)),
            _trade(4, ref="manual-note"),
            _trade(5, ref=""),
            _trade(6, ref=None),
            _trade(7, ref=_tag(5, 2), symbol="MSFT"),
        ]

    async def test_returns_only_decodable_legs_of_the_target_symbol(self) -> None:
        gateway, _ = _build(self._fixture())

        legs = await gateway.list_open_legs(SYMBOL)

        assert all(isinstance(leg, LegSnapshot) for leg in legs)
        assert {leg.client_order_id for leg in legs} == {"1", "2", "3"}

    async def test_snapshots_carry_state_filled_venue_id_and_group(self) -> None:
        gateway, _ = _build(self._fixture())

        by_id = {leg.client_order_id: leg for leg in await gateway.list_open_legs(SYMBOL)}

        assert by_id["1"] == LegSnapshot(
            client_order_id="1",
            link_tag=_tag(5, 0),
            state=OrderState.PARTIALLY_FILLED,
            filled=_D("0.4"),
            venue_order_id="11",
            group_id="5",
        )
        assert (by_id["2"].state, by_id["2"].filled) == (OrderState.OPEN, _D(0))

    async def test_group_filter_selects_only_that_group(self) -> None:
        gateway, _ = _build(self._fixture())

        g5 = await gateway.list_open_legs(SYMBOL, group="5")
        g6 = await gateway.list_open_legs(SYMBOL, group="6")

        assert {leg.client_order_id for leg in g5} == {"1", "2"}
        assert {leg.client_order_id for leg in g6} == {"3"}

    async def test_group_none_returns_all_linked_legs(self) -> None:
        gateway, _ = _build(self._fixture())

        assert len(await gateway.list_open_legs(SYMBOL, group=None)) == 3

    async def test_unknown_group_is_empty(self) -> None:
        gateway, _ = _build(self._fixture())

        assert list(await gateway.list_open_legs(SYMBOL, group="99")) == []

    async def test_other_symbol_leg_listed_under_its_own_target(self) -> None:
        gateway, _ = _build(self._fixture())

        legs = await gateway.list_open_legs("MSFT")

        assert {leg.client_order_id for leg in legs} == {"7"}

    async def test_completed_orders_are_never_listed_or_requested(self) -> None:
        gateway, ib = _build([_trade(1, ref=_tag(5, 0))], [_trade(2, ref=_tag(5, 1))])

        legs = await gateway.list_open_legs(SYMBOL)

        assert {leg.client_order_id for leg in legs} == {"1"}
        ib.reqCompletedOrdersAsync.assert_not_awaited()
