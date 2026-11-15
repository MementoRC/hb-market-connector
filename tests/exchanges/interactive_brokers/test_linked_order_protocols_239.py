"""IB conforms additively to SupportsBracket / SupportsOco and the handle shapes (#239).

Assumptions: the generic OCO method is ``place_oco``; IB keeps ``place_oca`` and gains
``place_oco`` as an additive alias returning ``IbOcaGroup``. Handle field names are those of
``market_connector.BracketHandle`` / ``OcoHandle`` (see the unit-level _239 test module);
IB leg ids are the ints of ``OrderHandle.client_order_id``.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

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

    def test_ib_bracket_has_every_bracket_handle_attribute(self) -> None:
        b = _ib_bracket()
        for f in dataclasses.fields(market_connector.BracketHandle):
            assert hasattr(b, f.name), f.name

    def test_ib_oca_group_exposes_leg_client_ids(self) -> None:
        g = _ib_oca()
        assert tuple(g.leg_client_ids) == (7, 8)
        assert g.emulated is False

    def test_ib_oca_group_has_every_oco_handle_attribute(self) -> None:
        g = _ib_oca()
        for f in dataclasses.fields(market_connector.OcoHandle):
            assert hasattr(g, f.name), f.name

    def test_ib_types_remain_frozen_and_keep_original_fields(self) -> None:
        b, g = _ib_bracket(), _ib_oca()
        assert (b.parent.client_order_id, g.group, g.oca_type) == (
            1,
            "g",
            OcaType.CANCEL_WITH_BLOCK,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            b.parent = _h(9)  # type: ignore[misc]


class TestStrAndInstrumentRefAccepted:
    @pytest.mark.parametrize("target", ["AAPL", InstrumentRef("AAPL", InstrumentType.STOCK)])
    async def test_place_bracket_accepts_str_and_ref(
        self, gateway: IbGatewayGateway, resolver: MagicMock, target: Any
    ) -> None:
        result = await gateway.place_bracket(
            target,
            _order(TradeType.BUY, "100"),
            _order(TradeType.SELL, "110"),
            _order(TradeType.SELL, "95"),
        )
        assert isinstance(result, IbBracket)
        if isinstance(target, str):
            resolver.resolve_from_pair.assert_awaited_once_with("AAPL")
        else:
            resolver.resolve.assert_awaited_once_with(target)

    @pytest.mark.parametrize("target", ["AAPL", InstrumentRef("AAPL", InstrumentType.STOCK)])
    async def test_place_oco_accepts_str_and_ref(
        self, gateway: IbGatewayGateway, resolver: MagicMock, target: Any
    ) -> None:
        result = await gateway.place_oco(
            target, [_order(TradeType.SELL, "110"), _order(TradeType.SELL, "120")]
        )
        assert isinstance(result, IbOcaGroup)
        if isinstance(target, str):
            resolver.resolve_from_pair.assert_awaited_once_with("AAPL")
        else:
            resolver.resolve.assert_awaited_once_with(target)

    async def test_place_oca_unchanged_signature_and_return(
        self, gateway: IbGatewayGateway
    ) -> None:
        ref = InstrumentRef("AAPL", InstrumentType.STOCK)
        result = await gateway.place_oca(
            ref, [_order(TradeType.SELL, "110")], OcaType.REDUCE_NO_BLOCK
        )
        assert isinstance(result, IbOcaGroup)

    async def test_place_oco_forwards_oca_type_option(
        self, gateway: IbGatewayGateway, transport: MagicMock
    ) -> None:
        await gateway.place_oco(
            "AAPL",
            [_order(TradeType.SELL, "110"), _order(TradeType.SELL, "120")],
            options={"oca_type": OcaType.REDUCE_NO_BLOCK},
        )
        assert OcaType.REDUCE_NO_BLOCK in transport.place_oca.await_args.args + tuple(
            transport.place_oca.await_args.kwargs.values()
        )
