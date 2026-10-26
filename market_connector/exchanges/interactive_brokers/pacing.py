"""Interactive Brokers request pacing for historical data and matching symbols.

Sources: IB's "Historical Data Limitations" (historical_limitations) and
"Matching Symbols" (matching_symbols) API documentation.

* The hard historical limits apply only to bars of 30 secs or less; they are lifted
  for 1 min bars and up. For small bars IB allows at most 60 requests per 10 minutes
  (a BID_ASK request counts twice), at most 6 requests per contract/exchange/tickType
  per 2 seconds, and no identical request within 15 seconds.
* At most 50 historical requests may be open at once (all bar sizes).
* ``reqMatchingSymbols`` is limited to one request per second.

Outgoing messages are already throttled by ib_async (``Client.MaxRequests`` = 45/s)
plus ``+PACEAPI``, so there is deliberately no message limiter here.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from rate_limit import KeyedCooldown, RateLimit, RateLimiter

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Hashable

    from rate_limit.types import Clock, Sleep

HISTORICAL_LIMIT_ID = "ib:historical"
MATCHING_SYMBOLS_LIMIT_ID = "ib:matching_symbols"

_HISTORICAL_REQUESTS = 60
_HISTORICAL_WINDOW_SECONDS = 600.0
_CONTRACT_REQUESTS = 6
_CONTRACT_WINDOW_SECONDS = 2.0
_IDENTICAL_WINDOW_SECONDS = 15.0


def _is_small_bar(bar_size: str) -> bool:
    """True for bar sizes of 30 secs or less (the unit is seconds)."""
    parts = bar_size.split()
    return len(parts) > 1 and parts[1].startswith("sec")


class IbPacer:
    """Paces IB historical-data and matching-symbols requests."""

    def __init__(
        self,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        max_open_historical: int = 50,
    ) -> None:
        self._limiter = RateLimiter(clock=clock, sleep=sleep)
        self._limiter.add_limits(
            [
                RateLimit(
                    HISTORICAL_LIMIT_ID,
                    _HISTORICAL_REQUESTS,
                    _HISTORICAL_WINDOW_SECONDS,
                    kind="window",
                ),
                RateLimit(MATCHING_SYMBOLS_LIMIT_ID, 1, 1.0, kind="window"),
            ]
        )
        self._contract_cooldown = KeyedCooldown(
            _CONTRACT_REQUESTS, _CONTRACT_WINDOW_SECONDS, clock=clock, sleep=sleep
        )
        self._identical_cooldown = KeyedCooldown(
            1, _IDENTICAL_WINDOW_SECONDS, clock=clock, sleep=sleep
        )
        self._max_open_historical = max_open_historical
        self._open_slots: asyncio.Semaphore | None = None

    @property
    def limiter(self) -> RateLimiter:
        return self._limiter

    def _semaphore(self) -> asyncio.Semaphore:
        if self._open_slots is None:
            self._open_slots = asyncio.Semaphore(self._max_open_historical)
        return self._open_slots

    @asynccontextmanager
    async def historical(
        self,
        *,
        contract_key: Hashable,
        exchange: str,
        what_to_show: str,
        bar_size: str,
        request_key: Hashable,
    ) -> AsyncIterator[None]:
        """Hold an open-request slot for the body, pacing small-bar requests first."""
        async with self._semaphore():
            if _is_small_bar(bar_size):
                weight = 2 if what_to_show == "BID_ASK" else 1
                await self._limiter.acquire(HISTORICAL_LIMIT_ID, {HISTORICAL_LIMIT_ID: weight})
                tick_key = (contract_key, exchange, what_to_show)
                await self._contract_cooldown.acquire(tick_key, weight)
                await self._identical_cooldown.acquire(request_key)
            yield

    async def matching_symbols(self) -> None:
        """Wait until a ``reqMatchingSymbols`` request is allowed."""
        await self._limiter.acquire(MATCHING_SYMBOLS_LIMIT_ID)

    def report_pacing_violation(self, retry_after: float | None = None) -> None:
        """Pause the historical limit after IB reported a pacing violation."""
        self._limiter.report_throttled(HISTORICAL_LIMIT_ID, retry_after)
