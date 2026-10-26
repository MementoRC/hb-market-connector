"""Kraken OrdersMixin routes AddOrder/CancelOrder through per-pair matching limits (#111)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from market_connector.exchanges.kraken.endpoints import ENDPOINT_REGISTRY
from market_connector.exchanges.kraken.mixins.orders import OrdersMixin
from market_connector.exchanges.kraken.specs import KRAKEN_RATE_LIMIT_SPEC
from market_connector.primitives import OrderType, TradeType
from market_connector.rate_limits.limiter import limiter_from_tiered_spec
from market_connector.transport.rest_base import RestConnectorBase

if TYPE_CHECKING:
    from collections.abc import Iterator

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "rest"
PAIR = "XBTUSD"


def _load(name: str) -> dict:
    with (_FIXTURES / name).open() as fh:
        return json.load(fh)


class _Orders(OrdersMixin):
    def __init__(self, rest: RestConnectorBase) -> None:
        self._rest = rest
        self._endpoints: dict = {}

    @property
    def ready(self) -> bool:
        return True


@pytest.fixture
def rest() -> RestConnectorBase:
    limiter = limiter_from_tiered_spec(KRAKEN_RATE_LIMIT_SPEC, "STARTER")
    return RestConnectorBase(
        "https://api.kraken.com", endpoints=ENDPOINT_REGISTRY, rate_limiter=limiter
    )


@pytest.fixture
def send(rest: RestConnectorBase) -> Iterator[AsyncMock]:
    def _respond(*, url: str, **_: object) -> httpx.Response:
        name = "cancel_order.json" if url.endswith("CancelOrder") else "add_order.json"
        return httpx.Response(200, json=_load(name))

    with patch.object(rest, "_client") as mock_client:
        mock_client.request = AsyncMock(side_effect=_respond)
        yield mock_client.request


class TestOrdersLimitId:
    async def test_place_order_uses_pair_limit_id(
        self, rest: RestConnectorBase, send: AsyncMock
    ) -> None:
        with patch.object(rest, "request", wraps=rest.request) as spy:
            await _Orders(rest).place_order(PAIR, OrderType.MARKET, TradeType.BUY, Decimal("1"))

        call = spy.call_args
        assert call.args[0] == "add_order"
        sent_pair = call.kwargs["data"]["pair"]
        assert call.kwargs["limit_id"] == f"add_order:{sent_pair}"
        assert rest._rate_limiter.has_limit(f"matching:{sent_pair}")

    async def test_cancel_order_uses_pair_limit_id(
        self, rest: RestConnectorBase, send: AsyncMock
    ) -> None:
        with patch.object(rest, "request", wraps=rest.request) as spy:
            await _Orders(rest).cancel_order(PAIR, "OAVY7T-MV5VK-KHDF5X")

        call = spy.call_args
        assert call.args[0] == "cancel_order"
        assert call.kwargs["limit_id"] == f"cancel_order:{PAIR}"
        assert rest._rate_limiter.has_limit(f"matching:{PAIR}")

    async def test_add_and_cancel_share_the_pair_pool(
        self, rest: RestConnectorBase, send: AsyncMock
    ) -> None:
        orders = _Orders(rest)
        await orders.place_order(PAIR, OrderType.MARKET, TradeType.BUY, Decimal("1"))
        await orders.cancel_order(PAIR, "OAVY7T-MV5VK-KHDF5X")

        limiter = rest._rate_limiter
        assert limiter.has_limit(f"add_order:{PAIR}")
        assert limiter.has_limit(f"cancel_order:{PAIR}")
        assert limiter.has_limit(f"matching:{PAIR}")
