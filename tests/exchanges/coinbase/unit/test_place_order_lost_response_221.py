"""Lost Coinbase place_order response surfaces OrderOutcomeUnknownError (#221), RED.

``OrderOutcomeUnknownError`` is new (subclass of ``ExchangeUnavailableError``), so this
file fails at import until it exists. Other #221 cases live in ``test_order_recovery_221.py``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest

from market_connector.exceptions import (
    ExchangeUnavailableError,
    OrderOutcomeUnknownError,  # new symbol
    OrderRejectedError,
)
from market_connector.exchanges.coinbase.mixins.orders import OrdersMixin
from market_connector.primitives import OrderType, TradeType
from market_connector.testing.mock_transport import MockRestClient


class _FailingRest(MockRestClient):
    """MockRestClient that records calls and raises ``exc`` on ``place_order``."""

    def __init__(self, exc: Exception | None = None) -> None:
        super().__init__()
        self.exc = exc
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def request(self, endpoint_name: str, params=None, data=None, headers=None):
        self.calls.append((endpoint_name, dict(data or params or {})))
        if endpoint_name == "place_order" and self.exc is not None:
            raise self.exc
        return await super().request(endpoint_name, params, data, headers)

    def sent(self, endpoint: str) -> list[dict[str, Any]]:
        return [body for name, body in self.calls if name == endpoint]


class _Gateway(OrdersMixin):
    def __init__(self, rest: _FailingRest) -> None:
        self._rest = rest
        self._endpoints = {}

    @property
    def ready(self) -> bool:
        return True


async def _place(gw: _Gateway, **kwargs: Any) -> str:
    return await gw.place_order(
        "BTC-USD", OrderType.LIMIT, TradeType.BUY, Decimal("0.5"), Decimal("50000"), **kwargs
    )


_LOST = [
    ExchangeUnavailableError("503 Service Unavailable"),
    httpx.ConnectError("connection reset"),
    httpx.ReadTimeout("response lost"),
]
_IDS = ["unavailable", "connect-error", "read-timeout"]


def test_outcome_unknown_is_an_exchange_unavailable_error() -> None:
    assert issubclass(OrderOutcomeUnknownError, ExchangeUnavailableError)


@pytest.mark.parametrize("exc", _LOST, ids=_IDS)
async def test_lost_response_raises_outcome_unknown_with_sent_client_id(exc: Exception) -> None:
    rest = _FailingRest(exc)

    with pytest.raises(OrderOutcomeUnknownError) as info:
        await _place(_Gateway(rest))

    sent = rest.sent("place_order")
    assert len(sent) == 1
    assert info.value.client_order_id == sent[0]["client_order_id"]
    assert info.value.client_order_id.startswith("coinbase-")


@pytest.mark.parametrize("exc", _LOST, ids=_IDS)
async def test_lost_response_keeps_a_caller_supplied_client_id(exc: Exception) -> None:
    rest = _FailingRest(exc)

    with pytest.raises(OrderOutcomeUnknownError) as info:
        await _place(_Gateway(rest), client_order_id="my-cid-1")

    assert info.value.client_order_id == "my-cid-1"
    assert [b["client_order_id"] for b in rest.sent("place_order")] == ["my-cid-1"]


@pytest.mark.parametrize("exc", _LOST, ids=_IDS)
async def test_lost_response_is_never_resent(exc: Exception) -> None:
    rest = _FailingRest(exc)

    with pytest.raises(ExchangeUnavailableError):
        await _place(_Gateway(rest))

    assert len(rest.sent("place_order")) == 1


async def test_definite_rejection_is_not_outcome_unknown() -> None:
    rest = _FailingRest()
    rest.register("place_order", {"success": False, "failure_reason": "INSUFFICIENT_FUND"})

    with pytest.raises(OrderRejectedError) as info:
        await _place(_Gateway(rest))

    assert not isinstance(info.value, OrderOutcomeUnknownError)
    assert len(rest.sent("place_order")) == 1
