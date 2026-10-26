"""Unit tests for IbPacer: IB historical-data and matching-symbols pacing (#108 slice c)."""

from __future__ import annotations

import asyncio

import pytest


class FakeClock:
    """Manual clock whose sleep advances time and records every wait."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay

    def advance(self, seconds: float) -> None:
        self.now += seconds

    @property
    def total_slept(self) -> float:
        return sum(self.sleeps)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def _pacer(clock: FakeClock, **kwargs):
    from market_connector.exchanges.interactive_brokers.pacing import IbPacer

    return IbPacer(clock=clock, sleep=clock.sleep, **kwargs)


async def _hist(
    pacer,
    *,
    contract_key: object = 1,
    exchange: str = "SMART",
    what_to_show: str = "TRADES",
    bar_size: str = "5 secs",
    request_key: object = None,
    _n: list[int] = [0],  # noqa: B006 - unique default keys across calls
) -> None:
    if request_key is None:
        _n[0] += 1
        request_key = ("auto", _n[0])
    async with pacer.historical(
        contract_key=contract_key,
        exchange=exchange,
        what_to_show=what_to_show,
        bar_size=bar_size,
        request_key=request_key,
    ):
        pass


class TestRegisteredLimits:
    def test_limit_ids(self) -> None:
        from market_connector.exchanges.interactive_brokers import pacing

        assert pacing.HISTORICAL_LIMIT_ID == "ib:historical"
        assert pacing.MATCHING_SYMBOLS_LIMIT_ID == "ib:matching_symbols"

    def test_historical_limit_is_60_per_600s_window(self, clock: FakeClock) -> None:
        from market_connector.exchanges.interactive_brokers.pacing import HISTORICAL_LIMIT_ID

        limiter = _pacer(clock).limiter
        assert limiter.has_limit(HISTORICAL_LIMIT_ID)
        limit = limiter.get_limit(HISTORICAL_LIMIT_ID)
        assert limit.limit == 60
        assert limit.time_interval == 600.0
        assert limit.kind == "window"

    def test_matching_symbols_limit_is_1_per_second_window(self, clock: FakeClock) -> None:
        from market_connector.exchanges.interactive_brokers.pacing import (
            MATCHING_SYMBOLS_LIMIT_ID,
        )

        limit = _pacer(clock).limiter.get_limit(MATCHING_SYMBOLS_LIMIT_ID)
        assert limit.limit == 1
        assert limit.time_interval == 1.0
        assert limit.kind == "window"


class TestHistoricalWindow:
    @pytest.mark.parametrize("bar_size", ["1 secs", "5 secs", "30 secs"])
    async def test_sixty_small_bar_requests_pass_61st_waits(
        self, clock: FakeClock, bar_size: str
    ) -> None:
        pacer = _pacer(clock)
        for i in range(60):
            await _hist(pacer, contract_key=i, bar_size=bar_size, request_key=("r", i))
        assert clock.sleeps == []

        await _hist(pacer, contract_key=999, bar_size=bar_size, request_key=("r", 999))
        assert clock.sleeps, "61st small-bar request must wait for the 10-minute window"

    async def test_bid_ask_counts_double(self, clock: FakeClock) -> None:
        pacer = _pacer(clock)
        for i in range(30):
            await _hist(pacer, contract_key=i, what_to_show="BID_ASK", request_key=("r", i))
        assert clock.sleeps == []

        await _hist(pacer, contract_key=999, what_to_show="TRADES", request_key=("r", 999))
        assert clock.sleeps, "30 BID_ASK requests (weight 2 each) exhaust the 60 limit"

    async def test_violation_report_blocks_next_small_bar_request(self, clock: FakeClock) -> None:
        pacer = _pacer(clock)
        pacer.report_pacing_violation(10.0)

        await _hist(pacer)

        assert clock.total_slept >= 10.0

    async def test_violation_report_without_retry_after_is_accepted(self, clock: FakeClock) -> None:
        _pacer(clock).report_pacing_violation()


class TestHistoricalPerContractCooldown:
    async def test_seventh_same_contract_request_waits_about_two_seconds(
        self, clock: FakeClock
    ) -> None:
        pacer = _pacer(clock)
        for i in range(6):
            await _hist(pacer, contract_key=7, request_key=("r", i))
        assert clock.sleeps == []

        await _hist(pacer, contract_key=7, request_key=("r", 6))
        assert clock.total_slept == pytest.approx(2.0, abs=0.05)

    async def test_different_contract_is_not_blocked(self, clock: FakeClock) -> None:
        pacer = _pacer(clock)
        for i in range(6):
            await _hist(pacer, contract_key=7, request_key=("r", i))

        await _hist(pacer, contract_key=8, request_key=("other", 0))
        assert clock.sleeps == []

    async def test_different_what_to_show_is_not_blocked(self, clock: FakeClock) -> None:
        pacer = _pacer(clock)
        for i in range(6):
            await _hist(pacer, contract_key=7, what_to_show="TRADES", request_key=("r", i))

        await _hist(pacer, contract_key=7, what_to_show="MIDPOINT", request_key=("m", 0))
        assert clock.sleeps == []

    async def test_different_exchange_is_not_blocked(self, clock: FakeClock) -> None:
        pacer = _pacer(clock)
        for i in range(6):
            await _hist(pacer, contract_key=7, exchange="SMART", request_key=("r", i))

        await _hist(pacer, contract_key=7, exchange="NYSE", request_key=("n", 0))
        assert clock.sleeps == []


class TestHistoricalIdenticalRequest:
    async def test_identical_request_within_15s_waits(self, clock: FakeClock) -> None:
        pacer = _pacer(clock)
        await _hist(pacer, request_key="same")
        assert clock.sleeps == []

        clock.advance(5.0)
        await _hist(pacer, request_key="same")
        assert clock.total_slept == pytest.approx(10.0, abs=0.05)

    async def test_identical_request_allowed_after_15s(self, clock: FakeClock) -> None:
        pacer = _pacer(clock)
        await _hist(pacer, request_key="same")

        clock.advance(15.0 + 0.001)  # hb-rate-limit's window boundary is inclusive
        await _hist(pacer, request_key="same")
        assert clock.sleeps == []

    async def test_distinct_request_keys_do_not_collide(self, clock: FakeClock) -> None:
        pacer = _pacer(clock)
        await _hist(pacer, request_key="a")
        await _hist(pacer, request_key="b")
        assert clock.sleeps == []


class TestLargeBarsAreExempt:
    @pytest.mark.parametrize("bar_size", ["1 min", "1 hour", "1 day"])
    async def test_large_bars_never_wait(self, clock: FakeClock, bar_size: str) -> None:
        pacer = _pacer(clock)
        for _ in range(100):
            await _hist(pacer, contract_key=7, bar_size=bar_size, request_key="same")
        assert clock.sleeps == []

    async def test_large_bars_do_not_consume_small_bar_budget(self, clock: FakeClock) -> None:
        pacer = _pacer(clock)
        for _ in range(100):
            await _hist(pacer, contract_key=7, bar_size="1 min", request_key="same")
        for i in range(60):
            await _hist(pacer, contract_key=i, bar_size="5 secs", request_key=("r", i))
        assert clock.sleeps == []


class TestOpenRequestSemaphore:
    async def test_third_concurrent_request_waits_for_a_slot(self, clock: FakeClock) -> None:
        pacer = _pacer(clock, max_open_historical=2)
        release = asyncio.Event()
        entered: list[int] = []

        async def hold(i: int) -> None:
            async with pacer.historical(
                contract_key=i,
                exchange="SMART",
                what_to_show="TRADES",
                bar_size="1 min",
                request_key=("r", i),
            ):
                entered.append(i)
                await release.wait()

        tasks = [asyncio.create_task(hold(i)) for i in range(3)]
        for _ in range(10):
            await asyncio.sleep(0)
        assert sorted(entered) == [0, 1]

        release.set()
        await asyncio.gather(*tasks)
        assert sorted(entered) == [0, 1, 2]

    async def test_exception_releases_the_slot(self, clock: FakeClock) -> None:
        pacer = _pacer(clock, max_open_historical=1)

        with pytest.raises(RuntimeError, match="boom"):
            async with pacer.historical(
                contract_key=1,
                exchange="SMART",
                what_to_show="TRADES",
                bar_size="1 min",
                request_key="k",
            ):
                raise RuntimeError("boom")

        await asyncio.wait_for(_hist(pacer, bar_size="1 min", request_key="k2"), timeout=1.0)


class TestMatchingSymbols:
    async def test_second_call_within_one_second_waits(self, clock: FakeClock) -> None:
        pacer = _pacer(clock)
        await pacer.matching_symbols()
        assert clock.sleeps == []

        await pacer.matching_symbols()
        assert clock.sleeps, "2nd matching-symbols call within 1 s must wait"
        assert clock.total_slept == pytest.approx(1.0, abs=0.05)

    async def test_call_after_one_second_passes(self, clock: FakeClock) -> None:
        pacer = _pacer(clock)
        await pacer.matching_symbols()
        clock.advance(1.0 + 0.001)  # hb-rate-limit's window boundary is inclusive
        await pacer.matching_symbols()
        assert clock.sleeps == []
