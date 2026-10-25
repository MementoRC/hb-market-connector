"""Acceptance tests: Coinbase flat / Kraken tiered pool specs enforced via hb-rate-limit (#108)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from rate_limit import RateLimit, RateLimiter
from rate_limit.testing import ManualClock

from market_connector.exchanges.coinbase.coinbase_gateway import CoinbaseGateway
from market_connector.exchanges.coinbase.config import CoinbaseConfig
from market_connector.exchanges.coinbase.endpoints import ENDPOINT_REGISTRY as COINBASE_ENDPOINTS
from market_connector.exchanges.coinbase.specs import COINBASE_RATE_LIMIT_SPEC
from market_connector.exchanges.kraken.endpoints import ENDPOINT_REGISTRY as KRAKEN_ENDPOINTS
from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway
from market_connector.exchanges.kraken.schemas.enums import KrakenAPITier
from market_connector.exchanges.kraken.specs import KRAKEN_RATE_LIMIT_SPEC
from market_connector.rate_limits.limiter import (
    limiter_from_flat_spec,
    limiter_from_tiered_spec,
    pool_to_rate_limit,
)
from market_connector.rate_limits.pool import PoolSpec
from market_connector.transport.endpoint import Endpoint
from market_connector.transport.rest_base import RestConnectorBase

PUBLIC_CB = ["products", "server_time", "product_book"]


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


def _client(
    limiter: RateLimiter, endpoints: dict[str, Endpoint]
) -> tuple[RestConnectorBase, AsyncMock]:
    client = RestConnectorBase(
        base_url="https://api.example.com", endpoints=endpoints, rate_limiter=limiter
    )
    send = AsyncMock(return_value=httpx.Response(200, json={}))
    client._client.request = send  # type: ignore[method-assign]
    return client, send


def _coinbase(clock: ManualClock) -> tuple[RestConnectorBase, RateLimiter]:
    limiter = limiter_from_flat_spec(COINBASE_RATE_LIMIT_SPEC, clock=clock, sleep=clock.sleep)
    client, _ = _client(limiter, COINBASE_ENDPOINTS)
    return client, limiter


def _kraken(clock: ManualClock, tier: str) -> tuple[RestConnectorBase, RateLimiter]:
    limiter = limiter_from_tiered_spec(KRAKEN_RATE_LIMIT_SPEC, tier, clock=clock, sleep=clock.sleep)
    client, _ = _client(limiter, KRAKEN_ENDPOINTS)
    return client, limiter


class TestPoolToRateLimit:
    def test_maps_fields_as_token_bucket(self) -> None:
        assert pool_to_rate_limit(PoolSpec("p", 10, 5.0)) == RateLimit(
            "p", 10, 2.0, kind="token_bucket"
        )

    def test_refill_equal_to_capacity_gives_one_second_interval(self) -> None:
        limit = pool_to_rate_limit(PoolSpec("private", 30, 30.0))
        assert (limit.limit_id, limit.limit, limit.time_interval) == ("private", 30, 1.0)
        assert limit.kind == "token_bucket"


class TestRegistryCoverage:
    def test_coinbase_spec_matches_registry(self) -> None:
        assert set(COINBASE_RATE_LIMIT_SPEC.endpoint_pools) == set(COINBASE_ENDPOINTS)

    def test_kraken_spec_matches_registry(self) -> None:
        assert set(KRAKEN_RATE_LIMIT_SPEC.endpoint_pools) == set(KRAKEN_ENDPOINTS)


class TestCoinbaseSharedPools:
    async def test_distinct_public_endpoints_share_one_pool(self, clock: ManualClock) -> None:
        client, _ = _coinbase(clock)

        for i in range(10):
            await client.request(PUBLIC_CB[i % 3])
        assert clock.sleeps == []

        await client.request("products")
        assert clock.sleeps, "11th public request must wait for the shared pool"
        await client.close()

    async def test_private_endpoint_unaffected_by_public_exhaustion(
        self, clock: ManualClock
    ) -> None:
        client, _ = _coinbase(clock)
        for i in range(10):
            await client.request(PUBLIC_CB[i % 3])

        await client.request("accounts")
        await client.request("place_order")

        assert clock.sleeps == []
        await client.close()

    async def test_bucket_refills_continuously(self, clock: ManualClock) -> None:
        client, limiter = _coinbase(clock)
        for i in range(10):
            await client.request(PUBLIC_CB[i % 3])
        assert limiter.blocked_for("products") > 0

        clock.advance(0.1 + 1e-6)  # 1 / refill_rate(10/s): one token back
        assert limiter.blocked_for("products") == 0
        await client.request("products")
        assert clock.sleeps == []
        assert limiter.blocked_for("server_time") > 0, "only one token should have refilled"
        await client.close()


class TestKrakenTiered:
    async def test_add_order_charges_private_and_matching(self, clock: ManualClock) -> None:
        client, limiter = _kraken(clock, "STARTER")
        assert limiter.blocked_for("matching", {"matching": 60}) == 0
        assert limiter.blocked_for("private", {"private": 15}) == 0

        await client.request("add_order")

        assert limiter.blocked_for("matching", {"matching": 60}) > 0
        assert limiter.blocked_for("private", {"private": 15}) > 0
        await client.close()

    async def test_balance_charges_private_only(self, clock: ManualClock) -> None:
        client, limiter = _kraken(clock, "STARTER")

        await client.request("balance")

        assert limiter.blocked_for("private", {"private": 15}) > 0
        assert limiter.blocked_for("matching", {"matching": 60}) == 0
        await client.close()

    async def test_cancel_order_charges_matching(self, clock: ManualClock) -> None:
        client, limiter = _kraken(clock, "STARTER")

        await client.request("cancel_order")

        assert limiter.blocked_for("matching", {"matching": 60}) > 0
        await client.close()

    async def test_trading_drains_private_but_public_still_passes(self, clock: ManualClock) -> None:
        client, _ = _kraken(clock, "STARTER")
        for _ in range(15):
            await client.request("add_order")
        assert clock.sleeps == []

        await client.request("server_time")  # separate public pool
        assert clock.sleeps == []

        await client.request("cancel_order")
        assert clock.sleeps, "private pool (cap 15) is exhausted"
        await client.close()

    async def test_starter_private_cap_is_15(self, clock: ManualClock) -> None:
        client, _ = _kraken(clock, "STARTER")
        for _ in range(15):
            await client.request("balance")
        assert clock.sleeps == []

        await client.request("balance")
        assert clock.sleeps
        await client.close()

    async def test_pro_private_cap_is_20(self, clock: ManualClock) -> None:
        client, _ = _kraken(clock, "PRO")
        for _ in range(16):
            await client.request("balance")
        assert clock.sleeps == []
        await client.close()

    def test_unknown_tier_raises(self) -> None:
        with pytest.raises(KeyError):
            limiter_from_tiered_spec(KRAKEN_RATE_LIMIT_SPEC, "NOPE")

    @pytest.mark.parametrize("name", sorted(KRAKEN_ENDPOINTS))
    async def test_every_registry_endpoint_is_requestable(
        self, clock: ManualClock, name: str
    ) -> None:
        client, _ = _kraken(clock, "STARTER")

        await client.request(name)

        await client.close()


class TestFallbackForUnregisteredName:
    async def test_name_not_in_limiter_uses_method_path_limit(self, clock: ManualClock) -> None:
        limiter = limiter_from_flat_spec(COINBASE_RATE_LIMIT_SPEC, clock=clock, sleep=clock.sleep)
        endpoints = {"mystery": Endpoint(path="/x", method="GET", limit=3, window=2.0)}
        client, _ = _client(limiter, endpoints)
        assert not limiter.has_limit("mystery")

        await client.request("mystery")

        assert limiter.get_raw_limit("GET:/x") == RateLimit("GET:/x", limit=3, time_interval=2.0)
        await client.close()

    async def test_spec_name_is_not_registered_as_method_path(self, clock: ManualClock) -> None:
        client, limiter = _coinbase(clock)

        await client.request("products")

        assert not limiter.has_limit("GET:/brokerage/market/products")
        await client.close()


class TestGatewayWiring:
    def test_coinbase_gateway_uses_spec_limiter(self) -> None:
        gw = CoinbaseGateway(CoinbaseConfig(api_key="k", secret_key="s", sandbox=True))
        limiter = gw._rest._rate_limiter
        assert not hasattr(gw, "_rate_limit")
        assert all(limiter.has_limit(name) for name in COINBASE_RATE_LIMIT_SPEC.endpoint_pools)
        assert limiter.get_raw_limit("public") == pool_to_rate_limit(
            COINBASE_RATE_LIMIT_SPEC.pools["public"]
        )

    @pytest.mark.parametrize("tier", [KrakenAPITier.STARTER, KrakenAPITier.PRO])
    def test_kraken_gateway_uses_tier_limiter(self, tier: KrakenAPITier) -> None:
        gw = KrakenGateway(api_key="k", secret_key="c2VjcmV0", tier=tier)
        limiter = gw._rest._rate_limiter
        assert not hasattr(gw, "_rate_limit")
        assert all(limiter.has_limit(name) for name in KRAKEN_RATE_LIMIT_SPEC.endpoint_pools)
        expected = KRAKEN_RATE_LIMIT_SPEC.tiers[str(tier)].pools["matching"]
        assert limiter.get_raw_limit("matching") == pool_to_rate_limit(expected)
