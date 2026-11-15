"""Coinbase offers no linked orders: it must not satisfy SupportsBracket / SupportsOco (#239)."""

from __future__ import annotations

import pytest

from market_connector import protocols
from market_connector.exchanges.coinbase.coinbase_gateway import CoinbaseGateway
from market_connector.exchanges.coinbase.config import CoinbaseConfig
from market_connector.testing import contract


@pytest.fixture
def gateway() -> CoinbaseGateway:
    return CoinbaseGateway(CoinbaseConfig(api_key="k", secret_key="raw_hmac_secret", sandbox=True))


def test_coinbase_is_not_supports_bracket(gateway: CoinbaseGateway) -> None:
    assert not isinstance(gateway, protocols.SupportsBracket)


def test_coinbase_is_not_supports_oco(gateway: CoinbaseGateway) -> None:
    assert not isinstance(gateway, protocols.SupportsOco)


def test_coinbase_linked_capability_contract_passes(gateway: CoinbaseGateway) -> None:
    assert contract.check_linked_protocol_capabilities(gateway) == []
