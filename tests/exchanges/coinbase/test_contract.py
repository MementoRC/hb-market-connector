"""Contract compliance tests — subclass of GatewayContractTestBase."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from market_connector.exchanges.coinbase.coinbase_gateway import CoinbaseGateway
from market_connector.exchanges.coinbase.config import CoinbaseConfig
from market_connector.testing.contract import GatewayContractTestBase
from market_connector.testing.mock_transport import MockRestClient, MockWsClient

if TYPE_CHECKING:
    from collections.abc import Generator
    from typing import Any


class _MockRestWithLifecycle(MockRestClient):
    """Extend MockRestClient with close() for gateway lifecycle tests."""

    async def close(self) -> None:
        pass


class _MockWsWithLifecycle(MockWsClient):
    """Extend MockWsClient with connect/disconnect for gateway lifecycle tests."""

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass


def _build_mock_rest() -> _MockRestWithLifecycle:
    mock_rest = _MockRestWithLifecycle()
    mock_rest.register(
        "server_time",
        {
            "iso": "2026-04-24T00:00:00Z",
            "epochSeconds": "1714000000",
            "epochMillis": "1714000000000",
        },
    )
    mock_rest.register(
        "accounts",
        {
            "accounts": [
                {
                    "uuid": "u",
                    "name": "USDT",
                    "currency": "USDT",
                    "available_balance": {"value": "1000", "currency": "USDT"},
                    "hold": {"value": "0", "currency": "USDT"},
                },
            ]
        },
    )
    mock_rest.register(
        "product_book",
        {
            "pricebook": {
                "product_id": "BTC-USD",
                "bids": [{"price": "50000", "size": "1"}],
                "asks": [{"price": "50001", "size": "1"}],
            }
        },
    )
    mock_rest.register(
        "candles",
        {
            "candles": [
                {
                    "start": "1714000000",
                    "low": "49000",
                    "high": "51000",
                    "open": "50000",
                    "close": "50500",
                    "volume": "10",
                },
            ]
        },
    )
    mock_rest.register(
        "place_order",
        {
            "success": True,
            "order_id": "o1",
            "success_response": {
                "order_id": "o1",
                "product_id": "BTC-USD",
                "side": "BUY",
                "client_order_id": "c1",
            },
        },
    )
    mock_rest.register(
        "cancel_orders",
        {"results": [{"success": True, "order_id": "o1"}]},
    )
    mock_rest.register("list_orders", {"orders": []})
    return mock_rest


class TestCoinbaseGatewayContract(GatewayContractTestBase):
    @pytest.fixture
    def gateway(self, monkeypatch: Any) -> Generator[CoinbaseGateway, None, None]:
        cfg = CoinbaseConfig(api_key="k", secret_key="raw_hmac_secret", sandbox=True)
        gw = CoinbaseGateway(cfg)
        gw._rest = _build_mock_rest()
        gw._ws = _MockWsWithLifecycle()
        yield gw

    @pytest.fixture
    def trading_pair(self) -> str:
        return "BTC-USD"
