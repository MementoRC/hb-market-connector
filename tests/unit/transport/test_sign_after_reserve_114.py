"""Acceptance tests: Coinbase signs (mints its JWT) only after rate-limit reserve (#114)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import httpx
import pytest
from rate_limit import RateLimit, RateLimiter
from rate_limit.testing import ManualClock

from market_connector.exchanges.coinbase.transport import CoinbaseRestClient
from market_connector.transport.endpoint import Endpoint

if TYPE_CHECKING:
    from market_connector.auth.protocols import Request

BASE = "https://api.example.com/v3"


class _OrderLog:
    """Shared, ordered record of reserve / sign / send events."""

    def __init__(self) -> None:
        self.events: list[str] = []


class _GatedLimiter(RateLimiter):
    """Real limiter whose reserve() blocks until the gate is released."""

    def __init__(self, log: _OrderLog, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._log = log
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()

    async def reserve(self, *args: Any, **kwargs: Any) -> Any:
        self._log.events.append("reserve:start")
        self.entered.set()
        await self.gate.wait()
        charge = await super().reserve(*args, **kwargs)
        self._log.events.append("reserve:done")
        return charge


class _SpySigner:
    """Stands in for the CDP JWT signer; records when it runs."""

    def __init__(self, log: _OrderLog, clock: ManualClock | None = None) -> None:
        self._log = log
        self._clock = clock
        self.calls: list[Request] = []
        self.sign_times: list[float] = []

    async def sign(self, request: Request) -> Request:
        self._log.events.append("sign")
        self.calls.append(request)
        if self._clock is not None:
            self.sign_times.append(self._clock.now)
        return replace(request, headers={**request.headers, "Authorization": "Bearer jwt"})


class _FailingSigner:
    async def sign(self, request: Request) -> Request:
        raise RuntimeError("jwt mint failed")


def _ok() -> httpx.Response:
    return httpx.Response(200, content=b"{}", headers={"content-type": "application/json"})


def _endpoints() -> dict[str, Endpoint]:
    return {
        "orders": Endpoint(path="/api/orders", method="GET", limit=1, window=10.0),
        "order": Endpoint(path="/api/orders/{order_id}", method="GET", limit=1, window=10.0),
    }


def _client(
    limiter: RateLimiter, signer: Any, log: _OrderLog | None = None
) -> tuple[CoinbaseRestClient, AsyncMock]:
    client = CoinbaseRestClient(
        base_url=BASE, endpoints=_endpoints(), signer=signer, rate_limiter=limiter
    )
    events = log.events if log is not None else []

    async def _send(**_: Any) -> httpx.Response:
        events.append("send")
        return _ok()

    send = AsyncMock(side_effect=_send)
    client._client.request = send  # type: ignore[method-assign]
    return client, send


class TestSignAfterReserve:
    async def test_signer_runs_only_after_blocked_reserve_returns(self) -> None:
        log = _OrderLog()
        limiter = _GatedLimiter(log)
        signer = _SpySigner(log)
        client, _ = _client(limiter, signer, log)

        task = asyncio.create_task(client.request("orders"))
        await asyncio.wait_for(limiter.entered.wait(), timeout=1)
        await asyncio.sleep(0)  # let anything that wrongly runs before reserve finish

        assert signer.calls == [], "JWT must not be minted while reserve() is still waiting"

        limiter.gate.set()
        await asyncio.wait_for(task, timeout=1)

        assert log.events == ["reserve:start", "reserve:done", "sign", "send"]
        await client.close()

    async def test_token_is_minted_after_the_rate_limit_wait(self) -> None:
        clock = ManualClock()
        limiter = RateLimiter(clock=clock, sleep=clock.sleep)
        signer = _SpySigner(_OrderLog(), clock)
        client, _ = _client(limiter, signer)

        await client.request("orders")  # consumes the only unit in the 10s window
        await client.request("orders")  # must wait ~10s for capacity

        assert clock.now >= 10.0
        assert len(signer.sign_times) == 2
        assert signer.sign_times[1] >= 10.0, "second JWT was minted before the wait ended"
        await client.close()


class TestRefundWhenSigningFails:
    async def test_signer_failure_refunds_and_sends_nothing(self) -> None:
        clock = ManualClock()
        log = _OrderLog()
        limiter = _GatedLimiter(log, clock=clock, sleep=clock.sleep)
        limiter.gate.set()
        limiter.add_limit(RateLimit("orders", 1, 10.0))  # charged under the endpoint name
        client, send = _client(limiter, _FailingSigner())

        with pytest.raises(RuntimeError, match="jwt mint failed"):
            await client.request("orders")
        send.assert_not_called()

        # A charge must have been reserved (signing happens after reserve) ...
        assert log.events == ["reserve:start", "reserve:done"]
        # ... and refunded: capacity is restored, so the next call does not wait.
        assert limiter.blocked_for("orders", {"orders": 1}) == 0
        assert clock.sleeps == []
        await client.close()


class TestSignedPathEqualsSentPath:
    async def test_templated_endpoint_signs_the_path_it_sends(self) -> None:
        log = _OrderLog()
        signer = _SpySigner(log)
        client, send = _client(RateLimiter(), signer, log)

        await client.request("order", params={"order_id": "a b", "limit": 3})

        sent_url = send.call_args.kwargs["url"]
        assert len(signer.calls) == 1
        assert "{" not in sent_url
        assert signer.calls[0].path == "/api/orders/a%20b"
        assert urlsplit(signer.calls[0].url).path == urlsplit(sent_url).path
        assert send.call_args.kwargs["params"] == {"limit": 3}
        await client.close()
