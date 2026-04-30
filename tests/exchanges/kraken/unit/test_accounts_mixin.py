"""Tests for Kraken AccountsMixin — Stage 4a."""

from __future__ import annotations

from decimal import Decimal

import pytest

from market_connector.exceptions import AuthenticationError, GatewayNotStartedError
from market_connector.exchanges.kraken.mixins.accounts import AccountsMixin
from market_connector.testing.mock_transport import MockRestClient

# ---------------------------------------------------------------------------
# Concrete test double
# ---------------------------------------------------------------------------


class _TestableAccounts(AccountsMixin):
    def __init__(self, rest: MockRestClient) -> None:
        self._rest = rest
        self._endpoints: dict = {}  # satisfies HasEndpoints, not used by mixin
        self._started = True

    @property
    def ready(self) -> bool:
        return self._started


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BALANCE_FIXTURE = {
    "error": [],
    "result": {
        "ZUSD": "171288.6158",
        "ZEUR": "504861.8946",
        "XXBT": "1011.1908877900",
        "XETH": "818.5500000000",
    },
}

_WS_TOKEN_FIXTURE = {
    "error": [],
    "result": {
        "token": "NMxvFC0z8OtYhKmYL/5dXoP7iXrW2izLsogu4FUzBPg",
        "expires": 900,
    },
}

_KRAKEN_ERROR_FIXTURE = {
    "error": ["EAPI:Invalid key"],
    "result": None,
}


# ---------------------------------------------------------------------------
# get_balance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_balance_finds_currency():
    rest = MockRestClient()
    rest.register("balance", _BALANCE_FIXTURE)
    mixin = _TestableAccounts(rest)
    assert await mixin.get_balance("XXBT") == Decimal("1011.1908877900")
    assert await mixin.get_balance("ZUSD") == Decimal("171288.6158")


@pytest.mark.asyncio
async def test_get_balance_missing_returns_zero():
    rest = MockRestClient()
    rest.register("balance", _BALANCE_FIXTURE)
    mixin = _TestableAccounts(rest)
    assert await mixin.get_balance("XDOGE") == Decimal("0")


@pytest.mark.asyncio
async def test_get_balance_not_ready_raises():
    rest = MockRestClient()
    mixin = _TestableAccounts(rest)
    mixin._started = False
    with pytest.raises(GatewayNotStartedError):
        await mixin.get_balance("XXBT")


# ---------------------------------------------------------------------------
# get_balances
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_balances_returns_all():
    rest = MockRestClient()
    rest.register("balance", _BALANCE_FIXTURE)
    mixin = _TestableAccounts(rest)
    balances = await mixin.get_balances()
    assert balances == {
        "ZUSD": Decimal("171288.6158"),
        "ZEUR": Decimal("504861.8946"),
        "XXBT": Decimal("1011.1908877900"),
        "XETH": Decimal("818.5500000000"),
    }


@pytest.mark.asyncio
async def test_get_balances_not_ready_raises():
    rest = MockRestClient()
    mixin = _TestableAccounts(rest)
    mixin._started = False
    with pytest.raises(GatewayNotStartedError):
        await mixin.get_balances()


# ---------------------------------------------------------------------------
# get_websockets_token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_websockets_token_returns_token_and_expiry():
    rest = MockRestClient()
    rest.register("get_websockets_token", _WS_TOKEN_FIXTURE)
    mixin = _TestableAccounts(rest)
    token, expires = await mixin.get_websockets_token()
    assert token == "NMxvFC0z8OtYhKmYL/5dXoP7iXrW2izLsogu4FUzBPg"
    assert expires == 900


@pytest.mark.asyncio
async def test_get_websockets_token_not_ready_raises():
    rest = MockRestClient()
    mixin = _TestableAccounts(rest)
    mixin._started = False
    with pytest.raises(GatewayNotStartedError):
        await mixin.get_websockets_token()


# ---------------------------------------------------------------------------
# Kraken error propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kraken_error_raises():
    rest = MockRestClient()
    rest.register("balance", _KRAKEN_ERROR_FIXTURE)
    mixin = _TestableAccounts(rest)
    # "EAPI:Invalid key" maps to AuthenticationError via raise_on_kraken_error
    with pytest.raises(AuthenticationError):
        await mixin.get_balance("XXBT")
