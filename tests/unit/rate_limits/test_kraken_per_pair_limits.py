"""Acceptance tests: Kraken decaying counters and per-pair matching limits (#111)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from rate_limit.testing import ManualClock

from market_connector.exchanges.kraken.specs import KRAKEN_RATE_LIMIT_SPEC
from market_connector.rate_limits.limiter import limiter_from_tiered_spec

if TYPE_CHECKING:
    from rate_limit import RateLimiter

EPS = 1e-6


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


def _limiter(clock: ManualClock, tier: str = "STARTER") -> RateLimiter:
    return limiter_from_tiered_spec(KRAKEN_RATE_LIMIT_SPEC, tier, clock=clock, sleep=clock.sleep)


def _keyed(limiter: RateLimiter, endpoint: str, key: str) -> str:
    from market_connector.rate_limits.limiter import ensure_keyed_limit

    return ensure_keyed_limit(
        limiter, endpoint, key, KRAKEN_RATE_LIMIT_SPEC.keyed_endpoint_pools[endpoint]
    )


def _drain(limiter: RateLimiter, limit_id: str, count: int) -> None:
    for _ in range(count):
        assert limiter.try_acquire(limit_id, {limit_id: 1}) == 0


class TestTemplateRegistration:
    def test_matching_pool_registered_as_template(self, clock: ManualClock) -> None:
        assert _limiter(clock).has_limit("matching")

    @pytest.mark.parametrize("name", ["add_order", "cancel_order"])
    def test_no_static_limit_for_keyed_endpoints(self, clock: ManualClock, name: str) -> None:
        assert not _limiter(clock).has_limit(name)


class TestEnsureKeyedLimit:
    def test_registers_pool_copy_and_endpoint_limit(self, clock: ManualClock) -> None:
        limiter = _limiter(clock)
        template = limiter.get_raw_limit("matching")

        lid = _keyed(limiter, "add_order", "XBTUSD")

        assert lid == "add_order:XBTUSD"
        copy = limiter.get_raw_limit("matching:XBTUSD")
        assert copy is not None
        assert (copy.limit, copy.time_interval, copy.kind) == (
            template.limit,
            template.time_interval,
            template.kind,
        )
        assert limiter.has_limit(lid)

    def test_endpoint_limit_links_keyed_pool_with_weight(self, clock: ManualClock) -> None:
        from market_connector.rate_limits.limiter import ensure_keyed_limit

        limiter = _limiter(clock)
        lid = ensure_keyed_limit(limiter, "add_order", "XBTUSD", [("matching", 3)])

        # weight 3 against a 60-capacity pool: 20 calls fit, the 21st does not
        _drain(limiter, lid, 20)
        assert limiter.blocked_for(lid, {lid: 1}) > 0

    def test_is_idempotent_and_does_not_reset(self, clock: ManualClock) -> None:
        limiter = _limiter(clock)
        lid = _keyed(limiter, "add_order", "XBTUSD")
        _drain(limiter, lid, 60)

        assert _keyed(limiter, "add_order", "XBTUSD") == lid

        assert limiter.blocked_for(lid, {lid: 1}) > 0

    def test_unknown_template_pool_raises_keyerror(self, clock: ManualClock) -> None:
        from market_connector.rate_limits.limiter import ensure_keyed_limit

        with pytest.raises(KeyError):
            ensure_keyed_limit(_limiter(clock), "add_order", "XBTUSD", [("nope", 1)])


class TestPerPairMatching:
    def test_exhausting_one_pair_does_not_block_another(self, clock: ManualClock) -> None:
        limiter = _limiter(clock)
        xbt = _keyed(limiter, "add_order", "XBTUSD")
        eth = _keyed(limiter, "add_order", "ETHUSD")

        _drain(limiter, xbt, 60)

        assert limiter.blocked_for(xbt, {xbt: 1}) > 0
        assert limiter.try_acquire(eth, {eth: 1}) == 0

    async def test_exhausted_pair_makes_reserve_sleep_other_pair_does_not(
        self, clock: ManualClock
    ) -> None:
        limiter = _limiter(clock)
        xbt = _keyed(limiter, "add_order", "XBTUSD")
        eth = _keyed(limiter, "add_order", "ETHUSD")
        for _ in range(60):
            await limiter.reserve(xbt, {xbt: 1})

        await limiter.reserve(eth, {eth: 1})
        assert clock.sleeps == []

        await limiter.reserve(xbt, {xbt: 1})
        assert clock.sleeps

    def test_pair_pool_decays_one_unit_per_second(self, clock: ManualClock) -> None:
        limiter = _limiter(clock)
        xbt = _keyed(limiter, "add_order", "XBTUSD")
        _drain(limiter, xbt, 60)

        clock.advance(1.0 + EPS)

        assert limiter.try_acquire(xbt, {xbt: 1}) == 0
        assert limiter.blocked_for(xbt, {xbt: 1}) > 0, "only ~1 unit regained, not 60"

    def test_cancel_order_shares_the_pair_pool_with_add_order(self, clock: ManualClock) -> None:
        limiter = _limiter(clock)
        add = _keyed(limiter, "add_order", "XBTUSD")
        cancel = _keyed(limiter, "cancel_order", "XBTUSD")

        _drain(limiter, add, 60)

        assert limiter.blocked_for(cancel, {cancel: 1}) > 0

    def test_add_order_does_not_drain_private(self, clock: ManualClock) -> None:
        limiter = _limiter(clock)
        xbt = _keyed(limiter, "add_order", "XBTUSD")

        _drain(limiter, xbt, 60)

        assert limiter.blocked_for("private", {"private": 15}) == 0


class TestPrivateDecay:
    def test_starter_private_refills_about_a_third_per_second(self, clock: ManualClock) -> None:
        limiter = _limiter(clock)
        _drain(limiter, "balance", 15)
        assert limiter.blocked_for("balance", {"balance": 1}) > 0

        clock.advance(1.0)
        assert limiter.blocked_for("balance", {"balance": 1}) > 0, "0.33 units is not enough"

        clock.advance(2.0 - EPS)  # 3.0 s total: still short of 1/0.33 = 3.03 s
        assert limiter.blocked_for("balance", {"balance": 1}) > 0

        clock.advance(0.1)  # 3.1 s total
        assert limiter.try_acquire("balance", {"balance": 1}) == 0

    def test_query_trades_consumes_two_units(self, clock: ManualClock) -> None:
        limiter = _limiter(clock)

        _drain(limiter, "query_trades", 7)  # 14 of 15 units

        assert limiter.blocked_for("query_trades", {"query_trades": 1}) > 0
