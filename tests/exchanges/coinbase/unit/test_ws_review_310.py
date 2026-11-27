"""Review-driven tests for #310: user-WS URL sandbox behaviour."""

from __future__ import annotations

import pytest

from market_connector.exchanges.coinbase.config import CoinbaseConfig

PROD_USER_WS = "wss://advanced-trade-ws-user.coinbase.com"


@pytest.mark.parametrize("sandbox", [False, True])
def test_ws_user_url_has_no_sandbox_variant(sandbox: bool) -> None:
    # Coinbase documents no sandbox user-WS host; the production host is used explicitly.
    cfg = CoinbaseConfig(api_key="k", secret_key="s", sandbox=sandbox)
    assert cfg.ws_user_url == PROD_USER_WS


def test_ws_url_still_honours_sandbox() -> None:
    assert "sandbox" in CoinbaseConfig(api_key="k", secret_key="s", sandbox=True).ws_url
    assert "sandbox" not in CoinbaseConfig(api_key="k", secret_key="s").ws_url
