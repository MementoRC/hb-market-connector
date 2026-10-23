"""Red acceptance tests for issue #57d — IB trading rules from contract details."""

from __future__ import annotations

# ruff: noqa: N815  (fakes mirror ib_async camelCase field names)
import math
import sys
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest

from market_connector.contracts.instrument import InstrumentRef, InstrumentType
from market_connector.exchanges.interactive_brokers.contract_resolver import IbContractResolver
from market_connector.exchanges.interactive_brokers.exceptions import ContractNotFoundError
from market_connector.exchanges.interactive_brokers.ib_gateway import IbGatewayGateway

UNSET = sys.float_info.max
STOCK_ORDER_TYPES = "ACTIVETIM,ADJUST,ALERT,LMT,MKT,STP"


def _rules_cls() -> Any:
    # Imported lazily so the missing module only fails tests in this file.
    from market_connector.exchanges.interactive_brokers.trading_rules import (  # noqa: PLC0415
        IbTradingRules,
    )

    return IbTradingRules


@dataclass
class FakeContract:
    secType: str = "STK"
    conId: int = 265598
    symbol: str = "AAPL"
    exchange: str = "SMART"
    primaryExchange: str = "NASDAQ"
    currency: str = "USD"


@dataclass
class FakeDetails:
    contract: FakeContract
    tradingHours: str = ""
    minTick: float = 0.01
    minSize: float = 1.0
    sizeIncrement: float = 1.0
    suggestedSizeIncrement: float = 1.0
    orderTypes: str = STOCK_ORDER_TYPES
    validExchanges: str = "SMART,NASDAQ"


def _details(con_id: int = 265598, **kw: Any) -> FakeDetails:
    return FakeDetails(FakeContract(conId=con_id), **kw)


def _ref(symbol: str = "AAPL") -> InstrumentRef:
    return InstrumentRef(symbol=symbol, instrument_type=InstrumentType.STOCK, quote_currency="USD")


class FakeTransport:
    def __init__(self, details: list[FakeDetails] | None = None) -> None:
        self._details = details or []
        self.resolve_calls = 0

    async def _resolve_via_ib(self, ref: InstrumentRef) -> list[FakeDetails]:
        self.resolve_calls += 1
        return self._details

    async def match_symbols(self, pattern: str) -> list[Any]:
        return []


def _gateway(transport: FakeTransport) -> IbGatewayGateway:
    resolver = IbContractResolver(transport)  # type: ignore[arg-type]
    return IbGatewayGateway(
        transport=MagicMock(),
        signer=MagicMock(),
        contract_resolver=resolver,
    )


class TestFromContractDetails:
    def test_stock_exact_decimals(self) -> None:
        rules = _rules_cls().from_contract_details(_details())
        assert rules.con_id == 265598
        assert rules.min_tick == Decimal("0.01")
        assert rules.size_increment == Decimal("1")
        assert rules.min_size == Decimal("1")
        assert rules.supports_market_orders is True
        assert rules.supports_limit_orders is True

    def test_crypto_fractional_exact(self) -> None:
        d = _details(minTick=0.01, sizeIncrement=0.0001, minSize=0.0001)
        rules = _rules_cls().from_contract_details(d)
        assert rules.min_tick == Decimal("0.01")
        assert rules.size_increment == Decimal("0.0001")
        assert rules.min_size == Decimal("0.0001")

    def test_frozen(self) -> None:
        rules = _rules_cls().from_contract_details(_details())
        with pytest.raises(AttributeError):
            rules.min_tick = Decimal("1")  # type: ignore[misc]

    @pytest.mark.parametrize("unset", [UNSET, 0.0, math.nan])
    def test_size_increment_unset_defaults_to_one(self, unset: float) -> None:
        rules = _rules_cls().from_contract_details(_details(sizeIncrement=unset))
        assert rules.size_increment == Decimal("1")

    def test_min_size_unset_falls_back_to_size_increment(self) -> None:
        d = _details(minSize=UNSET, sizeIncrement=0.0001)
        rules = _rules_cls().from_contract_details(d)
        assert rules.min_size == rules.size_increment == Decimal("0.0001")

    @pytest.mark.parametrize("unset", [UNSET, 0.0])
    def test_min_tick_unset_raises_naming_con_id(self, unset: float) -> None:
        with pytest.raises(ValueError, match="265598"):
            _rules_cls().from_contract_details(_details(minTick=unset))

    def test_no_mkt_means_no_market_orders(self) -> None:
        rules = _rules_cls().from_contract_details(_details(orderTypes="ALERT,LMT,STP"))
        assert rules.supports_market_orders is False
        assert rules.supports_limit_orders is True

    def test_order_types_parsed_stripped_no_empties(self) -> None:
        rules = _rules_cls().from_contract_details(_details(orderTypes=" LMT, MKT ,,STP,"))
        assert rules.order_types == frozenset({"LMT", "MKT", "STP"})


class TestResolverMetadata:
    @pytest.mark.asyncio
    async def test_contract_details_in_metadata(self) -> None:
        details = _details()
        resolved = await IbContractResolver(FakeTransport([details])).resolve(_ref())  # type: ignore[arg-type]
        assert resolved.metadata["contract_details"] is details

    @pytest.mark.asyncio
    async def test_existing_metadata_keys_unchanged(self) -> None:
        details = _details(tradingHours="20240101:0930-1600")
        resolved = await IbContractResolver(FakeTransport([details])).resolve(_ref())  # type: ignore[arg-type]
        assert resolved.metadata["primary_exchange"] == "NASDAQ"
        assert resolved.metadata["trading_hours"] == "20240101:0930-1600"


class TestGatewayGetTradingRules:
    @pytest.mark.asyncio
    async def test_returns_expected_rules(self) -> None:
        gw = _gateway(FakeTransport([_details()]))
        rules = await gw.get_trading_rules(_ref())  # type: ignore[attr-defined]
        assert rules == _rules_cls()(
            con_id=265598,
            min_tick=Decimal("0.01"),
            size_increment=Decimal("1"),
            min_size=Decimal("1"),
            supports_market_orders=True,
            supports_limit_orders=True,
            order_types=frozenset(STOCK_ORDER_TYPES.split(",")),
        )

    @pytest.mark.asyncio
    async def test_second_call_uses_cache(self) -> None:
        t = FakeTransport([_details()])
        gw = _gateway(t)
        first = await gw.get_trading_rules(_ref())  # type: ignore[attr-defined]
        second = await gw.get_trading_rules(_ref())  # type: ignore[attr-defined]
        assert first == second
        assert t.resolve_calls == 1

    @pytest.mark.asyncio
    async def test_not_found_propagates(self) -> None:
        gw = _gateway(FakeTransport([]))
        with pytest.raises(ContractNotFoundError):
            await gw.get_trading_rules(_ref("NOPE"))  # type: ignore[attr-defined]
