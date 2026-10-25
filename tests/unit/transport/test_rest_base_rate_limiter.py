"""Acceptance tests: RestConnectorBase throttles through one hb-rate-limit RateLimiter (#108)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import httpx
import pytest
from rate_limit import RateLimit, RateLimiter
from rate_limit.testing import ManualClock

from market_connector.exceptions import (
    AuthenticationError,
    ExchangeUnavailableError,
    RateLimitError,
)
from market_connector.transport.endpoint import Endpoint
from market_connector.transport.rest_base import RestConnectorBase

if TYPE_CHECKING:
    from market_connector.auth.protocols import Request

BOOK_ID = "GET:/api/book"
ORDERS_ID = "POST:/api/orders"


class _FailOnceSigner:
    """Signer that raises on the first call, then passes requests through."""

    def __init__(self) -> None:
        self.calls = 0

    async def sign(self, request: Request) -> Request:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("signing failed")
        return request


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def limiter(clock: ManualClock) -> RateLimiter:
    return RateLimiter(clock=clock, sleep=clock.sleep)


def _make_client(
    limiter: RateLimiter,
    endpoints: dict[str, Endpoint],
    send: AsyncMock | None = None,
    **kwargs: object,
) -> tuple[RestConnectorBase, AsyncMock]:
    client = RestConnectorBase(
        base_url="https://api.example.com",
        endpoints=endpoints,
        rate_limiter=limiter,
        **kwargs,  # type: ignore[arg-type]
    )
    send = send or AsyncMock(return_value=httpx.Response(200, json={}))
    client._client.request = send  # type: ignore[method-assign]
    return client, send


def _book(limit: int = 2, window: float = 1.0) -> dict[str, Endpoint]:
    return {"get_book": Endpoint(path="/api/book", method="GET", limit=limit, window=window)}


class TestInjectedLimiter:
    async def test_endpoint_registers_lazily_as_method_path(self, limiter: RateLimiter) -> None:
        client, _ = _make_client(limiter, _book(limit=3, window=2.0))
        assert not limiter.has_limit(BOOK_ID)

        await client.request("get_book")

        assert limiter.get_raw_limit(BOOK_ID) == RateLimit(BOOK_ID, limit=3, time_interval=2.0)
        await client.close()


class TestExhaustedLimitWaits:
    async def test_exhausted_limit_waits_instead_of_raising(
        self, limiter: RateLimiter, clock: ManualClock
    ) -> None:
        client, send = _make_client(limiter, _book(limit=2, window=1.0))

        for _ in range(3):
            await client.request("get_book")

        assert send.call_count == 3
        assert clock.sleeps, "third request should have waited for the window"
        assert clock.now >= 1.0
        await client.close()

    async def test_weight_consumes_that_many_units(
        self, limiter: RateLimiter, clock: ManualClock
    ) -> None:
        endpoints = {"place_order": Endpoint(path="/api/orders", method="POST", weight=2, limit=5)}
        client, _ = _make_client(limiter, endpoints)

        await client.request("place_order")
        await client.request("place_order")
        # 4 of 5 units used: a weight-2 call must wait, a weight-1 call would not.
        assert limiter.blocked_for(ORDERS_ID, {ORDERS_ID: 2}) > 0
        assert limiter.blocked_for(ORDERS_ID, {ORDERS_ID: 1}) == 0
        assert clock.sleeps == []

        await client.request("place_order")
        assert clock.sleeps, "third weight-2 request should have waited"
        await client.close()

    async def test_endpoints_have_independent_limits(
        self, limiter: RateLimiter, clock: ManualClock
    ) -> None:
        endpoints = {
            "a": Endpoint(path="/a", method="GET", limit=1),
            "b": Endpoint(path="/b", method="GET", limit=1),
        }
        client, _ = _make_client(limiter, endpoints)

        await client.request("a")
        await client.request("b")
        assert clock.sleeps == []

        await client.request("a")
        assert clock.sleeps, "second call on the same endpoint should wait"
        await client.close()


class TestRefundOnSignerFailure:
    async def test_signer_failure_refunds_charge_and_propagates(
        self, limiter: RateLimiter, clock: ManualClock
    ) -> None:
        signer = _FailOnceSigner()
        client, send = _make_client(limiter, _book(limit=1), signer=signer)

        with pytest.raises(RuntimeError, match="signing failed"):
            await client.request("get_book")
        assert send.call_count == 0

        await client.request("get_book")  # would wait if the charge were kept
        assert clock.sleeps == []
        assert send.call_count == 1
        await client.close()


class TestNoRefundAfterSend:
    @pytest.mark.parametrize(
        ("outcome", "expected"),
        [
            pytest.param(httpx.ReadTimeout("slow"), httpx.ReadTimeout, id="timeout"),
            pytest.param(httpx.Response(503, json={}), ExchangeUnavailableError, id="5xx"),
            pytest.param(httpx.Response(401, json={}), AuthenticationError, id="401"),
        ],
    )
    async def test_failure_after_sending_keeps_charge(
        self,
        limiter: RateLimiter,
        clock: ManualClock,
        outcome: Exception | httpx.Response,
        expected: type[Exception],
    ) -> None:
        send = AsyncMock(side_effect=[outcome, httpx.Response(200, json={})])
        client, _ = _make_client(limiter, _book(limit=1), send, max_retries=0, retry_delay=0.0)

        with pytest.raises(expected):
            await client.request("get_book")
        assert clock.sleeps == []

        await client.request("get_book")
        assert clock.sleeps, "charge for a sent request must not be refunded"
        await client.close()

    async def test_5xx_retries_do_not_recharge(
        self, limiter: RateLimiter, clock: ManualClock
    ) -> None:
        send = AsyncMock(
            side_effect=[
                httpx.Response(503, json={}),
                httpx.Response(503, json={}),
                httpx.Response(200, json={}),
            ]
        )
        client, _ = _make_client(limiter, _book(limit=1), send, max_retries=2, retry_delay=0.0)

        await client.request("get_book")

        assert send.call_count == 3
        assert clock.sleeps == []
        await client.close()


class TestThrottledResponse:
    async def test_429_reports_retry_after_to_limiter(
        self, limiter: RateLimiter, clock: ManualClock
    ) -> None:
        resp = httpx.Response(429, headers={"Retry-After": "7"}, json={})
        client, _ = _make_client(limiter, _book(limit=100), AsyncMock(return_value=resp))

        with pytest.raises(RateLimitError):
            await client.request("get_book")

        assert limiter.is_paused(BOOK_ID)
        assert limiter.blocked_for(BOOK_ID) >= 7 - 1e-6
        await client.close()

    async def test_next_request_waits_out_the_429_pause(
        self, limiter: RateLimiter, clock: ManualClock
    ) -> None:
        throttled = httpx.Response(429, headers={"Retry-After": "7"}, json={})
        send = AsyncMock(side_effect=[throttled, httpx.Response(200, json={})])
        client, _ = _make_client(limiter, _book(limit=100), send)

        with pytest.raises(RateLimitError):
            await client.request("get_book")
        await client.request("get_book")

        assert clock.now >= 7 - 1e-6
        await client.close()

    async def test_429_without_retry_after_still_raises(self, limiter: RateLimiter) -> None:
        resp = httpx.Response(429, json={})
        client, _ = _make_client(limiter, _book(limit=100), AsyncMock(return_value=resp))

        with pytest.raises(RateLimitError):
            await client.request("get_book")
        await client.close()
