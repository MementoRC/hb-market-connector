"""Tests for CoinbaseConfig — URL selection and immutability."""

import pytest

from market_connector.exchanges.coinbase.config import CoinbaseConfig


def test_config_production_urls() -> None:
    cfg = CoinbaseConfig(api_key="k", secret_key="s", sandbox=False)
    assert cfg.base_url == "https://api.coinbase.com/api/v3"
    assert cfg.ws_url == "wss://advanced-trade-ws.coinbase.com"


def test_config_sandbox_urls() -> None:
    cfg = CoinbaseConfig(api_key="k", secret_key="s", sandbox=True)
    assert "sandbox" in cfg.base_url
    assert "sandbox" in cfg.ws_url


def test_config_is_frozen() -> None:
    cfg = CoinbaseConfig(api_key="k", secret_key="s")
    with pytest.raises((AttributeError, ValueError)):
        cfg.api_key = "changed"  # type: ignore[misc]
