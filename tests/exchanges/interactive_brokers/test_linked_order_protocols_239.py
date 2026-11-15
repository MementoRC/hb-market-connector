"""IB conforms additively to SupportsBracket / SupportsOco and the handle shapes (#239).

Assumptions: the generic OCO method is ``place_oco``; IB keeps ``place_oca`` and gains
``place_oco`` as an additive alias returning ``IbOcaGroup``. Handle field names are those of
``market_connector.BracketHandle`` / ``OcoHandle`` (see the unit-level _239 test module);
IB leg ids are the ints of ``OrderHandle.client_order_id``.
"""

from __future__ import annotations

import dataclasses
import warnings
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from data_type_primitives import (
    LegRole,
    LegSpec,
    LinkedOrderSpec,
    LinkKind,
    TriggerKind,
    TriggerSpec,
)
from data_type_primitives import OrderType as DtpOrderType
from data_type_primitives import TradeType as DtpTradeType

import market_connector
from market_connector import protocols
from market_connector.capabilities import LinkedMode
from market_connector.contracts.instrument import InstrumentRef, InstrumentType
from market_connector.exchanges.interactive_brokers.ib_gateway import IbGatewayGateway
from market_connector.exchanges.interactive_brokers.linked_orders import (
    IbBracket,
    IbOcaGroup,
    OcaType,
)
from market_connector.orders import HBOrder, OrderType, TradeType
from market_connector.testing import contract

pytest.importorskip("ib_async")


def _h(client_id: int) -> Any:
    return SimpleNamespace(client_order_id=client_id)


def _ib_bracket() -> IbBracket:
    return IbBracket(parent=_h(1), take_profit=_h(2), stop_loss=_h(3))  # type: ignore[arg-type]


def _ib_oca() -> IbOcaGroup:
    return IbOcaGroup(
        group="g",
        oca_type=OcaType.CANCEL_WITH_BLOCK,
        orders=(_h(7), _h(8)),  # type: ignore[arg-type]
    )


def _order(side: TradeType, price: str) -> HBOrder:
    return HBOrder(order_type=OrderType.LIMIT, side=side, amount=Decimal(10), price=Decimal(price))


@pytest.fixture
def transport() -> MagicMock:
    t = MagicMock()
    t.place_bracket = AsyncMock(return_value=_ib_bracket())
    t.place_oca = AsyncMock(return_value=_ib_oca())
    return t


@pytest.fixture
def resolver() -> MagicMock:
    r = MagicMock()
    r.resolve = AsyncMock(return_value=SimpleNamespace(native="NATIVE"))
    r.resolve_from_pair = AsyncMock(return_value=SimpleNamespace(native="NATIVE"))
    return r


@pytest.fixture
def gateway(transport: MagicMock, resolver: MagicMock) -> IbGatewayGateway:
    return IbGatewayGateway(transport=transport, signer=MagicMock(), contract_resolver=resolver)


class TestStructuralConformance:
    def test_satisfies_supports_bracket(self, gateway: IbGatewayGateway) -> None:
        assert isinstance(gateway, protocols.SupportsBracket)

    def test_satisfies_supports_oco(self, gateway: IbGatewayGateway) -> None:
        assert isinstance(gateway, protocols.SupportsOco)

    def test_existing_ib_names_are_kept(self, gateway: IbGatewayGateway) -> None:
        assert callable(gateway.place_bracket)
        assert callable(gateway.place_oca)

    def test_advertises_bracket_and_oco(self, gateway: IbGatewayGateway) -> None:
        assert {LinkedMode.BRACKET, LinkedMode.OCO} <= gateway.order_capabilities.linked

    def test_linked_capability_contract_passes(self, gateway: IbGatewayGateway) -> None:
        assert contract.check_linked_protocol_capabilities(gateway) == []


class TestIbTypesUsableAsHandles:
    def test_ib_bracket_exposes_leg_client_ids(self) -> None:
        b = _ib_bracket()
        assert (b.entry_client_id, b.take_profit_client_id, b.stop_loss_client_id) == (1, 2, 3)
        assert b.emulated is False

    def test_ib_bracket_group_id_is_parent_order_id(self) -> None:
        assert _ib_bracket().group_id == 1

    def test_ib_oca_group_group_id_is_oca_group_name(self) -> None:
        assert _ib_oca().group_id == "g"

    def test_ib_bracket_satisfies_bracket_handle_protocol(self) -> None:
        assert isinstance(_ib_bracket(), market_connector.BracketHandle)
        assert not isinstance(_ib_oca(), market_connector.BracketHandle)

    def test_ib_bracket_with_single_exit_satisfies_bracket_handle(self) -> None:
        b = IbBracket(parent=_h(1), take_profit=_h(2), stop_loss=None)  # type: ignore[arg-type]
        assert isinstance(b, market_connector.BracketHandle)

    def test_ib_native_handles_are_frozen(self) -> None:
        for h in (_ib_bracket(), _ib_oca()):
            assert dataclasses.is_dataclass(h)
            with pytest.raises(dataclasses.FrozenInstanceError):
                h.emulated = True  # type: ignore[misc]

    def test_ib_oca_group_exposes_leg_client_ids(self) -> None:
        g = _ib_oca()
        assert tuple(g.leg_client_ids) == (7, 8)
        assert g.emulated is False

    def test_ib_oca_group_satisfies_oco_handle_protocol(self) -> None:
        assert isinstance(_ib_oca(), market_connector.OcoHandle)
        assert not isinstance(_ib_bracket(), market_connector.OcoHandle)

    def test_ib_types_remain_frozen_and_keep_original_fields(self) -> None:
        b, g = _ib_bracket(), _ib_oca()
        assert (b.parent.client_order_id, g.group, g.oca_type) == (
            1,
            "g",
            OcaType.CANCEL_WITH_BLOCK,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            b.parent = _h(9)  # type: ignore[misc]


def _dtp_leg(role: LegRole, side: DtpTradeType, price: str, ot: DtpOrderType) -> LegSpec:
    return LegSpec(
        role=role, order_type=ot, trade_type=side, amount=Decimal(10), price=Decimal(price)
    )


def _entry() -> LegSpec:
    return _dtp_leg(LegRole.ENTRY, DtpTradeType.BUY, "100", DtpOrderType.LIMIT)


def _tp() -> LegSpec:
    return _dtp_leg(LegRole.TAKE_PROFIT, DtpTradeType.SELL, "110", DtpOrderType.LIMIT)


def _sl_limit() -> LegSpec:
    return LegSpec(
        role=LegRole.STOP_LOSS,
        order_type=DtpOrderType.STOP_LOSS,
        trade_type=DtpTradeType.SELL,
        amount=Decimal(10),
        trigger=TriggerSpec(kind=TriggerKind.ABSOLUTE, value=Decimal(95)),
    )


def _bracket_spec(*exits: LegSpec) -> LinkedOrderSpec:
    return LinkedOrderSpec(kind=LinkKind.BRACKET, legs=(_entry(), *exits))


def _oco_spec() -> LinkedOrderSpec:
    return LinkedOrderSpec(
        kind=LinkKind.OCO,
        legs=(
            _dtp_leg(LegRole.OCO_LEG, DtpTradeType.SELL, "110", DtpOrderType.LIMIT),
            _dtp_leg(LegRole.OCO_LEG, DtpTradeType.SELL, "120", DtpOrderType.LIMIT),
        ),
    )


_TARGETS = ["AAPL", InstrumentRef("AAPL", InstrumentType.STOCK)]


def _assert_resolved(resolver: MagicMock, target: Any) -> None:
    if isinstance(target, str):
        resolver.resolve_from_pair.assert_awaited_once_with("AAPL")
    else:
        resolver.resolve.assert_awaited_once_with(target)


class TestIbBracketOptionalExits:
    def test_ib_bracket_allows_missing_take_profit(self) -> None:
        b = IbBracket(parent=_h(1), take_profit=None, stop_loss=_h(3))  # type: ignore[arg-type]
        assert (b.entry_client_id, b.take_profit_client_id, b.stop_loss_client_id) == (1, None, 3)

    def test_ib_bracket_allows_missing_stop_loss(self) -> None:
        b = IbBracket(parent=_h(1), take_profit=_h(2), stop_loss=None)  # type: ignore[arg-type]
        assert (b.entry_client_id, b.take_profit_client_id, b.stop_loss_client_id) == (1, 2, None)


class TestPlaceBracketSpecForm:
    @pytest.mark.parametrize("target", _TARGETS)
    async def test_accepts_str_and_ref(
        self, gateway: IbGatewayGateway, resolver: MagicMock, target: Any
    ) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            result = await gateway.place_bracket(target, _bracket_spec(_tp(), _sl_limit()))
        assert isinstance(result, IbBracket)
        _assert_resolved(resolver, target)

    @pytest.mark.parametrize(
        "exits", [(_tp(),), (_sl_limit(),), (_tp(), _sl_limit())], ids=["tp", "sl", "both"]
    )
    async def test_accepts_one_or_two_exits(
        self, gateway: IbGatewayGateway, transport: MagicMock, exits: tuple[LegSpec, ...]
    ) -> None:
        result = await gateway.place_bracket("AAPL", _bracket_spec(*exits))
        assert isinstance(result, IbBracket)
        transport.place_bracket.assert_awaited_once()

    async def test_options_keyword_accepted(self, gateway: IbGatewayGateway) -> None:
        result = await gateway.place_bracket("AAPL", _bracket_spec(_tp()), options={"x": 1})
        assert isinstance(result, IbBracket)


class TestPlaceBracketLegacyForm:
    @pytest.mark.parametrize("target", _TARGETS)
    async def test_legacy_per_leg_form_warns_and_still_works(
        self, gateway: IbGatewayGateway, resolver: MagicMock, target: Any
    ) -> None:
        with pytest.warns(DeprecationWarning):
            result = await gateway.place_bracket(
                target,
                _order(TradeType.BUY, "100"),
                _order(TradeType.SELL, "110"),
                _order(TradeType.SELL, "95"),
            )
        assert isinstance(result, IbBracket)
        _assert_resolved(resolver, target)

    async def test_legacy_form_forwards_orders_to_transport(
        self, gateway: IbGatewayGateway, transport: MagicMock
    ) -> None:
        entry, tp, sl = (
            _order(TradeType.BUY, "100"),
            _order(TradeType.SELL, "110"),
            _order(TradeType.SELL, "95"),
        )
        with pytest.warns(DeprecationWarning):
            await gateway.place_bracket("AAPL", entry, tp, sl)
        assert transport.place_bracket.await_args.args == ("NATIVE", entry, tp, sl)

    async def test_legacy_keyword_form_warns(self, gateway: IbGatewayGateway) -> None:
        with pytest.warns(DeprecationWarning):
            await gateway.place_bracket(
                "AAPL",
                entry=_order(TradeType.BUY, "100"),
                take_profit=_order(TradeType.SELL, "110"),
                stop_loss=_order(TradeType.SELL, "95"),
            )


class TestPlaceOco:
    @pytest.mark.parametrize("target", _TARGETS)
    async def test_accepts_str_and_ref(
        self, gateway: IbGatewayGateway, resolver: MagicMock, target: Any
    ) -> None:
        result = await gateway.place_oco(target, _oco_spec())
        assert isinstance(result, IbOcaGroup)
        _assert_resolved(resolver, target)

    async def test_transport_receives_legs_in_ordered_legs_order(
        self, gateway: IbGatewayGateway, transport: MagicMock
    ) -> None:
        spec = _oco_spec()
        await gateway.place_oco("AAPL", spec)
        orders = transport.place_oca.await_args.args[1]
        assert [o.price for o in orders] == [leg.price for leg in spec.ordered_legs()]

    async def test_forwards_oca_type_option(
        self, gateway: IbGatewayGateway, transport: MagicMock
    ) -> None:
        await gateway.place_oco("AAPL", _oco_spec(), options={"oca_type": OcaType.REDUCE_NO_BLOCK})
        assert OcaType.REDUCE_NO_BLOCK in transport.place_oca.await_args.args + tuple(
            transport.place_oca.await_args.kwargs.values()
        )

    async def test_default_oca_type_without_options(
        self, gateway: IbGatewayGateway, transport: MagicMock
    ) -> None:
        await gateway.place_oco("AAPL", _oco_spec())
        assert OcaType.CANCEL_WITH_BLOCK in transport.place_oca.await_args.args + tuple(
            transport.place_oca.await_args.kwargs.values()
        )


class TestPlaceOcaUnchanged:
    async def test_place_oca_unchanged_signature_and_return(
        self, gateway: IbGatewayGateway
    ) -> None:
        ref = InstrumentRef("AAPL", InstrumentType.STOCK)
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            result = await gateway.place_oca(
                ref, [_order(TradeType.SELL, "110")], OcaType.REDUCE_NO_BLOCK
            )
        assert isinstance(result, IbOcaGroup)


class TestNoCancelLinked:
    def test_protocols_do_not_declare_cancel_linked(self) -> None:
        assert not hasattr(protocols.SupportsBracket, "cancel_linked")
        assert not hasattr(protocols.SupportsOco, "cancel_linked")
