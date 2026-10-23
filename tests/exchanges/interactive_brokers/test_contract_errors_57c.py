"""Red acceptance tests for issue #57c — helpful not-found and ambiguous contract errors."""

from __future__ import annotations

# ruff: noqa: N815  (fakes mirror ib_async camelCase field names)
import sys
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from market_connector.contracts.instrument import InstrumentRef, InstrumentType
from market_connector.exchanges.interactive_brokers.contract_resolver import IbContractResolver
from market_connector.exchanges.interactive_brokers.exceptions import (
    AmbiguousContractError,
    ContractNotFoundError,
    IbError,
)
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.transport import IbGatewayTransport


@dataclass
class FakeContract:
    secType: str = "STK"
    conId: int = 0
    symbol: str = ""
    exchange: str = "SMART"
    primaryExchange: str = ""
    currency: str = "USD"


@dataclass
class FakeDetails:
    contract: FakeContract
    tradingHours: str = ""


@dataclass
class FakeDescription:
    contract: FakeContract
    derivativeSecTypes: list[str] = field(default_factory=list)


class FakeTransport:
    """Exposes the two transport methods the resolver may use and records calls."""

    def __init__(
        self,
        details: list[FakeDetails] | None = None,
        matches: list[FakeDescription] | None = None,
        match_error: Exception | None = None,
    ) -> None:
        self._details = details or []
        self._matches = matches or []
        self._match_error = match_error
        self.match_calls: list[str] = []
        self.resolve_calls = 0

    async def _resolve_via_ib(self, ref: InstrumentRef) -> list[FakeDetails]:
        self.resolve_calls += 1
        return self._details

    async def match_symbols(self, pattern: str) -> list[FakeDescription]:
        self.match_calls.append(pattern)
        if self._match_error is not None:
            raise self._match_error
        return self._matches


def _desc(symbol: str, exch: str, con_id: int, sec: str = "STK") -> FakeDescription:
    return FakeDescription(
        FakeContract(secType=sec, conId=con_id, symbol=symbol, primaryExchange=exch, currency="USD")
    )


def _detail(exchange: str, primary: str, con_id: int, symbol: str = "AAPL") -> FakeDetails:
    return FakeDetails(
        FakeContract(
            conId=con_id,
            symbol=symbol,
            exchange=exchange,
            primaryExchange=primary,
        )
    )


def _ref(symbol: str = "APPL", **kw: Any) -> InstrumentRef:
    return InstrumentRef(
        symbol=symbol, instrument_type=InstrumentType.STOCK, quote_currency="USD", **kw
    )


class TestNotFoundWithSuggestions:
    @pytest.mark.asyncio
    async def test_candidates_and_message(self) -> None:
        t = FakeTransport(matches=[_desc("AAPL", "NASDAQ", 265598), _desc("APLE", "NYSE", 111)])
        with pytest.raises(ContractNotFoundError) as ei:
            await IbContractResolver(t).resolve(_ref("APPL"))  # type: ignore[arg-type]
        assert t.match_calls == ["APPL"]
        first = "AAPL STK NASDAQ USD conId=265598"
        assert ei.value.candidates == (first, "APLE STK NYSE USD conId=111")
        assert "APPL" in str(ei.value)
        assert first in str(ei.value)

    @pytest.mark.asyncio
    async def test_candidates_capped_at_five(self) -> None:
        t = FakeTransport(matches=[_desc(f"S{i}", "NYSE", i) for i in range(8)])
        with pytest.raises(ContractNotFoundError) as ei:
            await IbContractResolver(t).resolve(_ref())  # type: ignore[arg-type]
        assert len(ei.value.candidates) == 5
        assert ei.value.candidates[0] == "S0 STK NYSE USD conId=0"

    @pytest.mark.asyncio
    async def test_zero_matches(self) -> None:
        t = FakeTransport(matches=[])
        with pytest.raises(ContractNotFoundError, match=r"(?i)no similar symbols") as ei:
            await IbContractResolver(t).resolve(_ref())  # type: ignore[arg-type]
        assert ei.value.candidates == ()
        assert t.match_calls == ["APPL"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("error", [RuntimeError("boom"), TimeoutError()])
    async def test_lookup_failure_never_escapes(self, error: Exception) -> None:
        t = FakeTransport(match_error=error)
        with pytest.raises(ContractNotFoundError) as ei:
            await IbContractResolver(t).resolve(_ref())  # type: ignore[arg-type]
        assert ei.value.candidates == ()
        assert t.match_calls == ["APPL"]

    @pytest.mark.asyncio
    async def test_con_id_path_skips_lookup(self) -> None:
        t = FakeTransport(matches=[_desc("AAPL", "NASDAQ", 265598)])
        with pytest.raises(ContractNotFoundError, match="999999") as ei:
            await IbContractResolver(t).resolve(_ref("APPL", con_id=999999))  # type: ignore[arg-type]
        assert t.match_calls == []
        assert ei.value.candidates == ()


class TestAmbiguousWithCandidates:
    @pytest.mark.asyncio
    async def test_candidates_and_hint(self) -> None:
        t = FakeTransport(details=[_detail("SMART", "NASDAQ", 1), _detail("SMART", "NYSE", 2)])
        with pytest.raises(AmbiguousContractError) as ei:
            await IbContractResolver(t).resolve(_ref("AAPL"))  # type: ignore[arg-type]
        assert ei.value.candidates == (
            "AAPL STK SMART NASDAQ USD conId=1",
            "AAPL STK SMART NYSE USD conId=2",
        )
        msg = str(ei.value)
        assert "primary_exchange" in msg
        assert "con_id" in msg
        assert t.match_calls == []

    @pytest.mark.asyncio
    async def test_candidates_capped_at_five(self) -> None:
        t = FakeTransport(details=[_detail("SMART", "NYSE", i) for i in range(7)])
        with pytest.raises(AmbiguousContractError) as ei:
            await IbContractResolver(t).resolve(_ref("AAPL"))  # type: ignore[arg-type]
        assert len(ei.value.candidates) == 5
        assert len(ei.value.matches) == 7


class TestGuards:
    @pytest.mark.asyncio
    async def test_single_match_resolves_without_lookup(self) -> None:
        t = FakeTransport(details=[_detail("SMART", "NASDAQ", 265598)])
        resolved = await IbContractResolver(t).resolve(_ref("AAPL"))  # type: ignore[arg-type]
        assert resolved.contract_id == "265598"
        assert t.match_calls == []

    def test_exception_types_unchanged(self) -> None:
        assert issubclass(ContractNotFoundError, IbError)
        assert issubclass(AmbiguousContractError, Exception)
        assert not issubclass(AmbiguousContractError, IbError)

    def test_legacy_construction_still_works(self) -> None:
        err = ContractNotFoundError(200, "No security definition")
        assert err.error_code == 200
        assert "No security definition" in str(err)
        assert getattr(err, "candidates", ()) == ()

    def test_legacy_ambiguous_construction_still_works(self) -> None:
        err = AmbiguousContractError(matches=[1, 2])
        assert err.matches == [1, 2]


class FakeIb:
    def __init__(self, result: list[Any] | None) -> None:
        self._result = result
        self.patterns: list[str] = []

    async def reqMatchingSymbolsAsync(self, pattern: str) -> list[Any] | None:  # noqa: N802
        self.patterns.append(pattern)
        return self._result


def _transport(fake_ib: FakeIb):
    spec = IbConnectionSpec(host="127.0.0.1", port=4002, client_id=1, paper=True)
    with (
        patch.dict(sys.modules, {"ib_async": MagicMock()}),
        patch(
            "market_connector.exchanges.interactive_brokers.transport.IB",
            return_value=fake_ib,
        ),
    ):
        return IbGatewayTransport(spec)


class TestTransportMatchSymbols:
    @pytest.mark.asyncio
    async def test_returns_ib_list(self) -> None:
        descs = [_desc("AAPL", "NASDAQ", 265598)]
        fake = FakeIb(descs)
        assert await _transport(fake).match_symbols("AAP") == descs
        assert fake.patterns == ["AAP"]

    @pytest.mark.asyncio
    async def test_none_becomes_empty_list(self) -> None:
        assert await _transport(FakeIb(None)).match_symbols("AAP") == []
