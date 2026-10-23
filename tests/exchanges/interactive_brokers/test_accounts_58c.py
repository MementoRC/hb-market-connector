"""Acceptance tests for issue #58c: account resolution, order account, balance, positions."""

from __future__ import annotations

import dataclasses
import importlib
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_connector.exchanges.interactive_brokers.ib_gateway import IbGatewayGateway
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec
from market_connector.exchanges.interactive_brokers.transport import (
    IbGatewayTransport,
    _hb_to_ib_order,
)
from market_connector.orders import HBOrder, OrderType, TradeType

from ._doubles import MockIb

pytest.importorskip("ib_async")

_IB_PATH = "market_connector.exchanges.interactive_brokers.transport.IB"
_PKG = "market_connector.exchanges.interactive_brokers"
ACCT = "DU1111111"
OTHER = "DU2222222"


def _account_config_error() -> type[Exception]:
    # Looked up lazily so collection succeeds while the class is missing (red phase).
    return getattr(importlib.import_module(f"{_PKG}.exceptions"), "IbAccountConfigError")  # noqa: B009


def _ib_position_cls() -> type:
    return importlib.import_module(f"{_PKG}.positions").IbPosition


def _av(tag: str, value: str, currency: str, account: str = ACCT) -> Any:
    from ib_async import AccountValue  # noqa: PLC0415

    return AccountValue(account, tag, value, currency, "")


def _balance_rows(account: str = ACCT) -> list[Any]:
    return [
        _av("AccountType", "INDIVIDUAL", "", account),
        _av("AvailableFunds", "1000.50", "USD", account),
        _av("TotalCashValue", "1200.00", "USD", account),
        _av("TotalCashValue", "300.25", "EUR", account),
        _av("TotalCashValue", "1530.10", "BASE", account),
    ]


def _contract(**kw: Any) -> Any:
    from ib_async import Contract  # noqa: PLC0415

    return Contract(**kw)


def _pos(contract: Any, qty: float, avg: float, account: str = ACCT) -> Any:
    from ib_async import Position  # noqa: PLC0415

    return Position(account, contract, qty, avg)


class CapturingIb(MockIb):
    """MockIb that records the order handed to placeOrder, then aborts the placement."""

    def __init__(self) -> None:
        super().__init__()
        self.placed_orders: list[Any] = []

    def placeOrder(self, contract: object, order: Any) -> Any:  # noqa: N802
        self.placed_orders.append(order)
        raise RuntimeError("stop after capture")


def _make(
    managed: list[str], account_id: str | None = None
) -> tuple[IbGatewayTransport, CapturingIb]:
    ib = CapturingIb()
    ib.managed_accounts = managed
    with patch(_IB_PATH, return_value=ib):
        transport = IbGatewayTransport(IbConnectionSpec(account_id=account_id))
    return transport, ib


def _market_order() -> HBOrder:
    return HBOrder(order_type=OrderType.MARKET, side=TradeType.BUY, amount=Decimal(10))


class TestAccountResolution:
    def test_before_connect_is_spec_account(self) -> None:
        transport, _ = _make([ACCT, OTHER], account_id=OTHER)
        assert transport.account == OTHER

    def test_before_connect_without_spec_is_none(self) -> None:
        transport, _ = _make([ACCT])
        assert transport.account is None

    async def test_single_account_resolved(self) -> None:
        transport, _ = _make([ACCT])
        await transport.connect()
        assert transport.account == ACCT

    async def test_configured_account_in_multi_list(self) -> None:
        transport, _ = _make([ACCT, OTHER], account_id=OTHER)
        await transport.connect()
        assert transport.account == OTHER

    async def test_connect_kwargs_unchanged(self) -> None:
        transport, ib = _make([ACCT, OTHER], account_id=OTHER)
        await transport.connect()
        assert ib.connect_calls[-1]["account"] == OTHER

    async def test_connect_kwargs_unchanged_without_account(self) -> None:
        transport, ib = _make([ACCT])
        await transport.connect()
        assert ib.connect_calls[-1]["account"] == ""

    async def test_multiple_accounts_unconfigured_raises(self) -> None:
        transport, ib = _make([ACCT, OTHER])
        with pytest.raises(_account_config_error()) as ei:
            await transport.connect()
        assert ACCT in str(ei.value)
        assert OTHER in str(ei.value)
        assert ib.disconnect_calls >= 1
        assert ib.isConnected() is False

    async def test_unknown_configured_account_raises(self) -> None:
        transport, ib = _make([ACCT, OTHER], account_id="DU9999999")
        with pytest.raises(_account_config_error()) as ei:
            await transport.connect()
        message = str(ei.value)
        assert "DU9999999" in message
        assert ACCT in message
        assert OTHER in message
        assert ib.disconnect_calls >= 1
        assert ib.isConnected() is False


class TestOrdersCarryAccount:
    async def test_place_order_sets_account(self) -> None:
        transport, ib = _make([ACCT])
        await transport.connect()
        with pytest.raises(Exception):  # noqa: B017, PT011 - placeOrder double aborts
            await transport.place_order(MagicMock(), _market_order())
        assert len(ib.placed_orders) == 1
        assert ib.placed_orders[0].account == ACCT

    async def test_place_order_uses_configured_account(self) -> None:
        transport, ib = _make([ACCT, OTHER], account_id=OTHER)
        await transport.connect()
        with pytest.raises(Exception):  # noqa: B017, PT011 - placeOrder double aborts
            await transport.place_order(MagicMock(), _market_order())
        assert ib.placed_orders[0].account == OTHER

    def test_hb_order_has_no_account_field(self) -> None:
        assert "account_id" not in HBOrder.model_fields

    def test_mapping_alone_needs_no_account(self) -> None:
        ib_order = _hb_to_ib_order(_market_order())
        assert ib_order.account == ""


class TestBalance:
    async def _balance(self, currency: str, rows: list[Any] | None = None) -> tuple[Decimal, Any]:
        transport, ib = _make([ACCT])
        ib.account_value_rows = _balance_rows() if rows is None else rows
        await transport.connect()
        return await transport.get_balance(currency), ib

    @pytest.mark.parametrize(
        ("currency", "expected"),
        [
            ("USD", Decimal("1000.50")),
            ("EUR", Decimal("300.25")),
            ("BASE", Decimal("1530.10")),
            ("JPY", Decimal("0")),
        ],
    )
    async def test_balance_rule(self, currency: str, expected: Decimal) -> None:
        value, _ = await self._balance(currency)
        assert value == expected

    async def test_currency_case_insensitive(self) -> None:
        value, _ = await self._balance("usd")
        assert value == Decimal("1000.50")

    async def test_returns_decimal(self) -> None:
        value, _ = await self._balance("EUR")
        assert isinstance(value, Decimal)

    async def test_queries_resolved_account(self) -> None:
        _, ib = await self._balance("USD")
        assert ib.account_values_calls == [ACCT]

    async def test_other_account_rows_ignored(self) -> None:
        rows = [_av("AvailableFunds", "999999", "USD", OTHER), *_balance_rows()]
        value, _ = await self._balance("USD", rows)
        assert value == Decimal("1000.50")

    async def test_other_account_only_gives_zero(self) -> None:
        rows = [_av("AvailableFunds", "999999", "USD", OTHER)]
        value, _ = await self._balance("USD", rows)
        assert value == Decimal("0")

    async def test_no_rows_gives_zero(self) -> None:
        value, _ = await self._balance("USD", [])
        assert value == Decimal("0")


class TestGatewayDelegation:
    async def test_get_balance_delegates(self) -> None:
        transport = MagicMock()
        transport.get_balance = AsyncMock(return_value=Decimal("42.5"))
        gateway = IbGatewayGateway(transport=transport, signer=MagicMock())
        assert await gateway.get_balance("USD") == Decimal("42.5")
        transport.get_balance.assert_awaited_once_with("USD")

    async def test_get_positions_delegates(self) -> None:
        sentinel = [object()]
        transport = MagicMock()
        transport.get_positions = AsyncMock(return_value=sentinel)
        gateway = IbGatewayGateway(transport=transport, signer=MagicMock())
        assert await gateway.get_positions() is sentinel
        transport.get_positions.assert_awaited_once_with()


class TestPositions:
    async def _positions(self, rows: list[Any]) -> tuple[list[Any], CapturingIb]:
        transport, ib = _make([ACCT])
        ib.position_rows = rows
        await transport.connect()
        return await transport.get_positions(), ib

    async def test_long_stock_mapping(self) -> None:
        aapl = _contract(
            symbol="AAPL",
            secType="STK",
            conId=265598,
            exchange="",
            primaryExchange="NASDAQ",
            currency="USD",
        )
        positions, _ = await self._positions([_pos(aapl, 10.0, 150.25)])
        assert positions == [
            _ib_position_cls()(
                account=ACCT,
                symbol="AAPL",
                sec_type="STK",
                con_id=265598,
                exchange="NASDAQ",
                currency="USD",
                quantity=Decimal("10.0"),
                avg_cost=Decimal("150.25"),
            )
        ]

    async def test_exchange_preferred_over_primary(self) -> None:
        es = _contract(
            symbol="ES", secType="FUT", conId=1, exchange="CME", primaryExchange="X", currency="USD"
        )
        positions, _ = await self._positions([_pos(es, -2.0, 50000.0)])
        assert positions[0].exchange == "CME"

    async def test_short_future_negative_quantity(self) -> None:
        es = _contract(symbol="ES", secType="FUT", conId=495512563, exchange="CME", currency="USD")
        positions, _ = await self._positions([_pos(es, -2.0, 50000.0)])
        assert positions[0].sec_type == "FUT"
        assert positions[0].quantity == Decimal("-2.0")

    async def test_fractional_quantity_exact(self) -> None:
        btc = _contract(symbol="BTC", secType="CRYPTO", conId=2, exchange="PAXOS", currency="USD")
        positions, _ = await self._positions([_pos(btc, 0.5, 100.0)])
        assert positions[0].quantity == Decimal("0.5")
        assert isinstance(positions[0].avg_cost, Decimal)

    async def test_queries_resolved_account(self) -> None:
        _, ib = await self._positions([])
        assert ib.positions_calls == [ACCT]

    async def test_empty(self) -> None:
        positions, _ = await self._positions([])
        assert positions == []

    def test_ib_position_is_frozen(self) -> None:
        cls = _ib_position_cls()
        pos = cls(
            account=ACCT,
            symbol="AAPL",
            sec_type="STK",
            con_id=1,
            exchange="SMART",
            currency="USD",
            quantity=Decimal(1),
            avg_cost=Decimal(1),
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            pos.symbol = "MSFT"  # type: ignore[misc]
