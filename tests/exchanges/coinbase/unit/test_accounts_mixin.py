"""Tests for AccountsMixin — Phase 6, Task 6.1."""

from decimal import Decimal

import pytest

from market_connector.exceptions import GatewayNotStartedError
from market_connector.exchanges.coinbase.mixins.accounts import AccountsMixin
from market_connector.testing.mock_transport import MockRestClient


class _TestableAccounts(AccountsMixin):
    def __init__(self, rest):
        self._rest = rest
        self._endpoints = {}  # Not used by mock
        self._started = True

    @property
    def ready(self) -> bool:
        return self._started


@pytest.mark.asyncio
async def test_get_balance_finds_currency():
    rest = MockRestClient()
    rest.register(
        "accounts",
        {
            "accounts": [
                {
                    "uuid": "u1",
                    "name": "BTC",
                    "currency": "BTC",
                    "available_balance": {"value": "0.5", "currency": "BTC"},
                    "hold": {"value": "0", "currency": "BTC"},
                },
                {
                    "uuid": "u2",
                    "name": "USD",
                    "currency": "USD",
                    "available_balance": {"value": "1000", "currency": "USD"},
                    "hold": {"value": "0", "currency": "USD"},
                },
            ],
        },
    )
    mixin = _TestableAccounts(rest)
    assert await mixin.get_balance("BTC") == Decimal("0.5")
    assert await mixin.get_balance("USD") == Decimal("1000")


@pytest.mark.asyncio
async def test_get_balance_missing_currency_zero():
    rest = MockRestClient()
    rest.register("accounts", {"accounts": []})
    mixin = _TestableAccounts(rest)
    assert await mixin.get_balance("ETH") == Decimal("0")


@pytest.mark.asyncio
async def test_get_balance_not_ready_raises():
    rest = MockRestClient()
    mixin = _TestableAccounts(rest)
    mixin._started = False
    with pytest.raises(GatewayNotStartedError):
        await mixin.get_balance("BTC")
