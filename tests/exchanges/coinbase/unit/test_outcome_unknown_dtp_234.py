"""Coinbase lost place_order response is catchable as the dtp base error (#234), RED."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import data_type_primitives as dtp
import httpx
import pytest

from market_connector.exceptions import ExchangeUnavailableError
from market_connector.exchanges.coinbase.mixins.orders import OrdersMixin
from market_connector.primitives import OrderType, TradeType
from market_connector.testing.mock_transport import MockRestClient


class _FailingRest(MockRestClient):
    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self.exc = exc
        self.sent: list[dict[str, Any]] = []

    async def request(self, endpoint_name: str, params=None, data=None, headers=None):
        if endpoint_name == "place_order":
            self.sent.append(dict(data or params or {}))
            raise self.exc
        return await super().request(endpoint_name, params, data, headers)


class _Gateway(OrdersMixin):
    def __init__(self, rest: _FailingRest) -> None:
        self._rest = rest
        self._endpoints = {}

    @property
    def ready(self) -> bool:
        return True


_LOST = [
    ExchangeUnavailableError("503 Service Unavailable"),
    httpx.ConnectError("connection reset"),
    httpx.ReadTimeout("response lost"),
]
_IDS = ["unavailable", "connect-error", "read-timeout"]


async def _place(gw: _Gateway) -> str:
    return await gw.place_order(
        "BTC-USD", OrderType.LIMIT, TradeType.BUY, Decimal("0.5"), Decimal("50000")
    )


@pytest.mark.parametrize("exc", _LOST, ids=_IDS)
async def test_lost_response_is_catchable_as_dtp_type_with_client_order_id(
    exc: Exception,
) -> None:
    rest = _FailingRest(exc)

    with pytest.raises(dtp.OrderOutcomeUnknownError) as info:
        await _place(_Gateway(rest))

    assert info.value.client_order_id == rest.sent[0]["client_order_id"]


@pytest.mark.parametrize("exc", _LOST, ids=_IDS)
async def test_lost_response_still_catchable_as_exchange_unavailable(exc: Exception) -> None:
    with pytest.raises(ExchangeUnavailableError):
        await _place(_Gateway(_FailingRest(exc)))
