# Exchange Gateway Framework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the core exchange gateway framework in `hb-market-connector` -- protocols, primitives, exception hierarchy, transport base classes, contract test base, and hb_compat bridge.

**Architecture:** Four-layer separation (schema -> converter -> adapter -> gateway protocol) with typed exceptions, frozen Pydantic v2 primitives, and composable transport building blocks. The framework defines what connectors must implement; individual connector packages (e.g., `hb-coinbase-connector`) are a separate plan.

**Tech Stack:** Python 3.10+, Pydantic v2, asyncio, httpx, websockets, pytest + pytest-asyncio

**Spec:** `docs/superpowers/specs/2026-04-23-exchange-gateway-design.md`

**Scope:** `hb-market-connector` framework only. The reference connector (`hb-coinbase-connector`) will be a separate plan once the framework is validated.

**Working directory:** `/home/memento/PycharmProjects/Hummingbot/hummingbot/sub-packages/market-connector`

**Run commands with:** `pixi run test`, `pixi run lint`, `pixi run format`

---

## File Structure

### Source Files (all under `market_connector/`)

| File | Responsibility | Lines (est.) |
|------|---------------|-------------|
| `exceptions.py` | Exception hierarchy: 8 classes, all inheriting `GatewayError` | ~35 |
| `primitives.py` | Frozen Pydantic v2 models: `OrderType`, `TradeType`, `OpenOrder`, `TradeEvent`, `OrderBookSnapshot`, `OrderBookUpdate` | ~75 |
| `protocols.py` | `@runtime_checkable` protocols: `ExecutionGateway`, `MarketDataGateway`, `ExchangeGateway` | ~65 |
| `transport/__init__.py` | Re-exports: `Endpoint`, `TokenBucket`, `RestConnectorBase`, `WsConnectorBase` | ~10 |
| `transport/endpoint.py` | `Endpoint` dataclass for per-endpoint rate limit configuration | ~25 |
| `transport/token_bucket.py` | Async token bucket rate limiter | ~50 |
| `transport/rest_base.py` | `RestConnectorBase`: rate-limited async REST client with retry + auth hook | ~120 |
| `transport/ws_base.py` | `WsConnectorBase`: reconnecting async WS client with subscription registry | ~150 |
| `testing/__init__.py` | Re-exports: `GatewayContractTestBase`, `MockTransport` | ~5 |
| `testing/contract.py` | `GatewayContractTestBase`: abstract pytest class for connector conformance | ~100 |
| `testing/mock_transport.py` | `MockRestClient`, `MockWsClient` for component testing | ~60 |
| `hb_compat/__init__.py` | Re-exports: `LiveMarketAccess` | ~5 |
| `hb_compat/bridge.py` | `LiveMarketAccess`: sync wrapper adapting `ExchangeGateway` -> strategy-framework protocols | ~60 |
| `__init__.py` | Package-level public API exports | ~30 |

### Test Files (all under `tests/unit/`)

| File | What it tests |
|------|--------------|
| `test_exceptions.py` | Hierarchy, inheritance chain, message formatting |
| `test_primitives.py` | Model creation, immutability (frozen), field types, validation errors |
| `test_protocols.py` | Protocol structural subtyping: mock class satisfies / violates protocol |
| `test_endpoint.py` | Endpoint creation, defaults, validation |
| `test_token_bucket.py` | Acquire/release, exhaustion, refill timing |
| `test_rest_base.py` | Rate limiting, retry on 5xx, auth header injection, error mapping |
| `test_ws_base.py` | Connect/disconnect, reconnection, subscription lifecycle, message routing |
| `test_contract_base.py` | Contract test base exercised with a mock gateway |
| `test_hb_compat.py` | Sync/async bridge, timeout, thread safety |

### Dependencies to Add

Add to `pyproject.toml` under `[project.dependencies]`:
```toml
dependencies = [
    "hb-strategy-framework>=0.1.0",
    "httpx>=0.27",
    "websockets>=12.0",
]
```

Add to `pixi.toml` under `[dependencies]`:
```toml
httpx = ">=0.27"
websockets = ">=12.0"
```

---

## Task 1: Exception Hierarchy

**Files:**
- Create: `market_connector/exceptions.py`
- Test: `tests/unit/test_exceptions.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_exceptions.py
import pytest

from market_connector.exceptions import (
    AuthenticationError,
    ExchangeUnavailableError,
    GatewayError,
    GatewayNotStartedError,
    OrderNotFoundError,
    OrderRejectedError,
    RateLimitError,
    SubscriptionLimitError,
)


class TestExceptionHierarchy:
    """All gateway exceptions inherit from GatewayError."""

    @pytest.mark.parametrize(
        "exc_class",
        [
            GatewayNotStartedError,
            OrderRejectedError,
            OrderNotFoundError,
            RateLimitError,
            SubscriptionLimitError,
            AuthenticationError,
            ExchangeUnavailableError,
        ],
    )
    def test_subclass_of_gateway_error(self, exc_class: type) -> None:
        assert issubclass(exc_class, GatewayError)

    @pytest.mark.parametrize(
        "exc_class",
        [
            GatewayError,
            GatewayNotStartedError,
            OrderRejectedError,
            OrderNotFoundError,
            RateLimitError,
            SubscriptionLimitError,
            AuthenticationError,
            ExchangeUnavailableError,
        ],
    )
    def test_subclass_of_exception(self, exc_class: type) -> None:
        assert issubclass(exc_class, Exception)

    def test_message_preserved(self) -> None:
        err = OrderRejectedError("insufficient funds")
        assert str(err) == "insufficient funds"

    def test_gateway_error_catchall(self) -> None:
        """Catching GatewayError catches any subclass."""
        with pytest.raises(GatewayError):
            raise RateLimitError("too many requests")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/memento/PycharmProjects/Hummingbot/hummingbot/sub-packages/market-connector && pixi run test -- tests/unit/test_exceptions.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'market_connector.exceptions'`

- [ ] **Step 3: Write the implementation**

```python
# market_connector/exceptions.py
"""Typed exception hierarchy for the exchange gateway framework.

All gateway errors inherit from GatewayError, allowing consumers to catch
either specific errors or the base class as a catch-all.
"""


class GatewayError(Exception):
    """Base class for all gateway errors."""


class GatewayNotStartedError(GatewayError):
    """A gateway method was called before start()."""


class OrderRejectedError(GatewayError):
    """The exchange rejected the order (insufficient funds, invalid params, etc.)."""


class OrderNotFoundError(GatewayError):
    """Cancel or query for an order that does not exist."""


class RateLimitError(GatewayError):
    """REST transport exhausted its rate-limit budget for an endpoint."""


class SubscriptionLimitError(GatewayError):
    """WebSocket subscription cap exceeded for the exchange."""


class AuthenticationError(GatewayError):
    """Credentials are invalid, expired, or missing required permissions."""


class ExchangeUnavailableError(GatewayError):
    """The exchange is down, returning 5xx, or otherwise unreachable."""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/memento/PycharmProjects/Hummingbot/hummingbot/sub-packages/market-connector && pixi run test -- tests/unit/test_exceptions.py -v`
Expected: all 17 tests PASS (7 + 8 parameterized + 2 explicit)

- [ ] **Step 5: Commit**

```bash
git add market_connector/exceptions.py tests/unit/test_exceptions.py
git commit -m "feat: add typed exception hierarchy for gateway framework"
```

---

## Task 2: Domain Primitives

**Files:**
- Create: `market_connector/primitives.py`
- Test: `tests/unit/test_primitives.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_primitives.py
from decimal import Decimal

import pytest
from pydantic import ValidationError

from market_connector.primitives import (
    OpenOrder,
    OrderBookSnapshot,
    OrderBookUpdate,
    OrderType,
    TradeEvent,
    TradeType,
)


class TestEnums:
    def test_order_types(self) -> None:
        assert OrderType.LIMIT == "LIMIT"
        assert OrderType.MARKET == "MARKET"
        assert OrderType.LIMIT_MAKER == "LIMIT_MAKER"

    def test_trade_types(self) -> None:
        assert TradeType.BUY == "BUY"
        assert TradeType.SELL == "SELL"


class TestOpenOrder:
    def test_create(self) -> None:
        order = OpenOrder(
            client_order_id="c1",
            exchange_order_id="e1",
            trading_pair="BTC-USDT",
            order_type=OrderType.LIMIT,
            side=TradeType.BUY,
            amount=Decimal("1.5"),
            price=Decimal("50000"),
            filled_amount=Decimal("0"),
            status="OPEN",
        )
        assert order.trading_pair == "BTC-USDT"
        assert order.amount == Decimal("1.5")

    def test_frozen(self) -> None:
        order = OpenOrder(
            client_order_id="c1",
            exchange_order_id="e1",
            trading_pair="BTC-USDT",
            order_type=OrderType.LIMIT,
            side=TradeType.BUY,
            amount=Decimal("1.5"),
            price=Decimal("50000"),
            filled_amount=Decimal("0"),
            status="OPEN",
        )
        with pytest.raises(ValidationError):
            order.status = "FILLED"  # type: ignore[misc]


class TestTradeEvent:
    def test_create(self) -> None:
        event = TradeEvent(
            exchange_trade_id="t1",
            trading_pair="ETH-USD",
            price=Decimal("3000.50"),
            amount=Decimal("2.0"),
            side=TradeType.SELL,
            timestamp=1700000000.0,
        )
        assert event.exchange_trade_id == "t1"
        assert event.side == TradeType.SELL

    def test_frozen(self) -> None:
        event = TradeEvent(
            exchange_trade_id="t1",
            trading_pair="ETH-USD",
            price=Decimal("3000"),
            amount=Decimal("1"),
            side=TradeType.BUY,
            timestamp=1700000000.0,
        )
        with pytest.raises(ValidationError):
            event.price = Decimal("9999")  # type: ignore[misc]


class TestOrderBookSnapshot:
    def test_create(self) -> None:
        snap = OrderBookSnapshot(
            trading_pair="BTC-USDT",
            bids=[(Decimal("50000"), Decimal("1.0"))],
            asks=[(Decimal("50001"), Decimal("0.5"))],
            timestamp=1700000000.0,
        )
        assert len(snap.bids) == 1
        assert snap.bids[0] == (Decimal("50000"), Decimal("1.0"))

    def test_empty_book(self) -> None:
        snap = OrderBookSnapshot(
            trading_pair="BTC-USDT", bids=[], asks=[], timestamp=0.0,
        )
        assert snap.bids == []

    def test_frozen(self) -> None:
        snap = OrderBookSnapshot(
            trading_pair="BTC-USDT", bids=[], asks=[], timestamp=0.0,
        )
        with pytest.raises(ValidationError):
            snap.timestamp = 999.0  # type: ignore[misc]


class TestOrderBookUpdate:
    def test_create(self) -> None:
        update = OrderBookUpdate(
            trading_pair="BTC-USDT",
            bids=[(Decimal("49999"), Decimal("2.0"))],
            asks=[],
            update_id=42,
        )
        assert update.update_id == 42

    def test_frozen(self) -> None:
        update = OrderBookUpdate(
            trading_pair="BTC-USDT", bids=[], asks=[], update_id=1,
        )
        with pytest.raises(ValidationError):
            update.update_id = 99  # type: ignore[misc]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/memento/PycharmProjects/Hummingbot/hummingbot/sub-packages/market-connector && pixi run test -- tests/unit/test_primitives.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the implementation**

```python
# market_connector/primitives.py
"""Frozen Pydantic v2 domain primitives for the exchange gateway framework.

All models are immutable (frozen=True). Connectors convert exchange-specific
schemas to these types in their converters.py module.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class OrderType(StrEnum):
    """Order type for gateway execution methods."""

    LIMIT = "LIMIT"
    MARKET = "MARKET"
    LIMIT_MAKER = "LIMIT_MAKER"


class TradeType(StrEnum):
    """Trade side for gateway execution methods."""

    BUY = "BUY"
    SELL = "SELL"


class OpenOrder(BaseModel):
    """An open order as reported by the exchange."""

    model_config = ConfigDict(frozen=True)

    client_order_id: str
    exchange_order_id: str
    trading_pair: str
    order_type: OrderType
    side: TradeType
    amount: Decimal
    price: Decimal
    filled_amount: Decimal
    status: str


class TradeEvent(BaseModel):
    """A single trade (fill or public trade)."""

    model_config = ConfigDict(frozen=True)

    exchange_trade_id: str
    trading_pair: str
    price: Decimal
    amount: Decimal
    side: TradeType
    timestamp: float


class OrderBookSnapshot(BaseModel):
    """Full order book from a REST endpoint."""

    model_config = ConfigDict(frozen=True)

    trading_pair: str
    bids: list[tuple[Decimal, Decimal]]
    asks: list[tuple[Decimal, Decimal]]
    timestamp: float


class OrderBookUpdate(BaseModel):
    """Incremental order book delta from a WebSocket stream."""

    model_config = ConfigDict(frozen=True)

    trading_pair: str
    bids: list[tuple[Decimal, Decimal]]
    asks: list[tuple[Decimal, Decimal]]
    update_id: int
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/memento/PycharmProjects/Hummingbot/hummingbot/sub-packages/market-connector && pixi run test -- tests/unit/test_primitives.py -v`
Expected: all 11 tests PASS (2 enum + 3 snapshot + 2 update + 2 order + 2 trade)

- [ ] **Step 5: Commit**

```bash
git add market_connector/primitives.py tests/unit/test_primitives.py
git commit -m "feat: add frozen Pydantic v2 domain primitives"
```

---

## Task 3: Gateway Protocols

**Files:**
- Create: `market_connector/protocols.py`
- Test: `tests/unit/test_protocols.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_protocols.py
"""Test that protocols are runtime-checkable and enforce structural subtyping."""

from __future__ import annotations

from contextlib import asynccontextmanager
from decimal import Decimal
from typing import AsyncIterator

import pytest

from market_connector.primitives import (
    OpenOrder,
    OrderBookSnapshot,
    OrderBookUpdate,
    TradeEvent,
)
from market_connector.protocols import (
    ExchangeGateway,
    ExecutionGateway,
    MarketDataGateway,
)


class _MockExecution:
    """Satisfies ExecutionGateway structurally."""

    async def place_order(self, trading_pair, order_type, side, amount, price):
        return "order-1"

    async def cancel_order(self, trading_pair, client_order_id):
        return True

    async def get_open_orders(self, trading_pair):
        return []

    async def get_balance(self, currency):
        return Decimal("100")


class _MockMarketData:
    """Satisfies MarketDataGateway structurally."""

    async def get_orderbook(self, trading_pair):
        return OrderBookSnapshot(trading_pair=trading_pair, bids=[], asks=[], timestamp=0.0)

    async def get_candles(self, trading_pair, interval, limit):
        return []

    async def get_mid_price(self, trading_pair):
        return Decimal("50000")

    async def subscribe_orderbook(self, trading_pair, callback):
        @asynccontextmanager
        async def _ctx() -> AsyncIterator[None]:
            yield
        return _ctx()

    async def subscribe_trades(self, trading_pair, callback):
        @asynccontextmanager
        async def _ctx() -> AsyncIterator[None]:
            yield
        return _ctx()


class _MockFullGateway(_MockExecution, _MockMarketData):
    """Satisfies ExchangeGateway structurally."""

    async def start(self):
        pass

    async def stop(self):
        pass

    @property
    def ready(self):
        return True


class _Incomplete:
    """Missing methods -- should NOT satisfy any protocol."""

    async def place_order(self, trading_pair, order_type, side, amount, price):
        return "order-1"


class TestExecutionGateway:
    def test_mock_satisfies_protocol(self) -> None:
        assert isinstance(_MockExecution(), ExecutionGateway)

    def test_incomplete_fails(self) -> None:
        assert not isinstance(_Incomplete(), ExecutionGateway)


class TestMarketDataGateway:
    def test_mock_satisfies_protocol(self) -> None:
        assert isinstance(_MockMarketData(), MarketDataGateway)

    def test_incomplete_fails(self) -> None:
        assert not isinstance(_Incomplete(), MarketDataGateway)


class TestExchangeGateway:
    def test_full_mock_satisfies(self) -> None:
        assert isinstance(_MockFullGateway(), ExchangeGateway)

    def test_execution_only_fails(self) -> None:
        assert not isinstance(_MockExecution(), ExchangeGateway)

    def test_market_data_only_fails(self) -> None:
        assert not isinstance(_MockMarketData(), ExchangeGateway)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/memento/PycharmProjects/Hummingbot/hummingbot/sub-packages/market-connector && pixi run test -- tests/unit/test_protocols.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the implementation**

```python
# market_connector/protocols.py
"""Runtime-checkable gateway protocols for the exchange gateway framework.

Connectors implement ExchangeGateway. Consumers depend on the protocol,
never on a concrete connector class.
"""

from __future__ import annotations

from decimal import Decimal
from typing import AsyncContextManager, Callable, Protocol, runtime_checkable

from market_connector.primitives import OpenOrder, OrderBookSnapshot, OrderBookUpdate, TradeEvent

# Note: @runtime_checkable isinstance checks verify method *names* only,
# not parameter types or return types. Use mypy for full type checking.
# CandleData is intentionally not imported here — get_candles returns list
# and each connector defines its own candle type. The hb_compat bridge
# handles conversion to strategy-framework's CandleData.


@runtime_checkable
class ExecutionGateway(Protocol):
    """Protocol for order execution operations."""

    async def place_order(
        self,
        trading_pair: str,
        order_type: str,
        side: str,
        amount: Decimal,
        price: Decimal | None,
    ) -> str: ...

    async def cancel_order(self, trading_pair: str, client_order_id: str) -> bool: ...

    async def get_open_orders(self, trading_pair: str) -> list[OpenOrder]: ...

    async def get_balance(self, currency: str) -> Decimal: ...


@runtime_checkable
class MarketDataGateway(Protocol):
    """Protocol for market data operations."""

    async def get_orderbook(self, trading_pair: str) -> OrderBookSnapshot: ...

    async def get_candles(self, trading_pair: str, interval: str, limit: int) -> list: ...

    async def get_mid_price(self, trading_pair: str) -> Decimal: ...

    async def subscribe_orderbook(
        self,
        trading_pair: str,
        callback: Callable[[OrderBookUpdate], None],
    ) -> AsyncContextManager: ...

    async def subscribe_trades(
        self,
        trading_pair: str,
        callback: Callable[[TradeEvent], None],
    ) -> AsyncContextManager: ...


@runtime_checkable
class ExchangeGateway(ExecutionGateway, MarketDataGateway, Protocol):
    """Composite protocol: execution + market data + lifecycle.

    This is what connector implementations must satisfy.
    """

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    @property
    def ready(self) -> bool: ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/memento/PycharmProjects/Hummingbot/hummingbot/sub-packages/market-connector && pixi run test -- tests/unit/test_protocols.py -v`
Expected: all 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add market_connector/protocols.py tests/unit/test_protocols.py
git commit -m "feat: add runtime-checkable gateway protocols"
```

---

## Task 4: Endpoint Model and Token Bucket

**Files:**
- Create: `market_connector/transport/__init__.py`
- Create: `market_connector/transport/endpoint.py`
- Create: `market_connector/transport/token_bucket.py`
- Test: `tests/unit/test_endpoint.py`
- Test: `tests/unit/test_token_bucket.py`

- [ ] **Step 1: Write the failing endpoint test**

```python
# tests/unit/test_endpoint.py
from market_connector.transport.endpoint import Endpoint


class TestEndpoint:
    def test_create_with_defaults(self) -> None:
        ep = Endpoint(path="/api/v3/orders", method="POST")
        assert ep.weight == 1
        assert ep.limit == 10
        assert ep.window == 1.0

    def test_create_with_overrides(self) -> None:
        ep = Endpoint(path="/api/v3/book", method="GET", weight=5, limit=20, window=2.0)
        assert ep.weight == 5
        assert ep.limit == 20
        assert ep.window == 2.0

    def test_immutable(self) -> None:
        ep = Endpoint(path="/api/v3/orders", method="POST")
        try:
            ep.weight = 99  # type: ignore[misc]
            raise AssertionError("Should be frozen")
        except AttributeError:
            pass  # expected — dataclass is frozen
```

- [ ] **Step 2: Write the failing token bucket test**

```python
# tests/unit/test_token_bucket.py
import asyncio

import pytest

from market_connector.transport.token_bucket import TokenBucket


class TestTokenBucket:
    @pytest.mark.asyncio
    async def test_acquire_within_limit(self) -> None:
        bucket = TokenBucket(rate=10, window=1.0)
        for _ in range(10):
            await bucket.acquire(weight=1)
        # Should not raise — 10 tokens available per window

    @pytest.mark.asyncio
    async def test_acquire_exhausted_raises(self) -> None:
        bucket = TokenBucket(rate=2, window=1.0)
        await bucket.acquire(weight=1)
        await bucket.acquire(weight=1)
        from market_connector.exceptions import RateLimitError

        with pytest.raises(RateLimitError, match="rate limit"):
            await bucket.acquire(weight=1)

    @pytest.mark.asyncio
    async def test_acquire_with_weight(self) -> None:
        bucket = TokenBucket(rate=10, window=1.0)
        await bucket.acquire(weight=5)
        await bucket.acquire(weight=5)
        from market_connector.exceptions import RateLimitError

        with pytest.raises(RateLimitError):
            await bucket.acquire(weight=1)

    @pytest.mark.asyncio
    async def test_tokens_refill_after_window(self) -> None:
        # Use 100ms window + 200ms sleep for CI tolerance (avoid flaky timing)
        bucket = TokenBucket(rate=1, window=0.1)
        await bucket.acquire(weight=1)
        from market_connector.exceptions import RateLimitError

        with pytest.raises(RateLimitError):
            await bucket.acquire(weight=1)
        await asyncio.sleep(0.2)  # Wait well past window for CI reliability
        await bucket.acquire(weight=1)  # Should succeed after refill
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd /home/memento/PycharmProjects/Hummingbot/hummingbot/sub-packages/market-connector && pixi run test -- tests/unit/test_endpoint.py tests/unit/test_token_bucket.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 4: Implement endpoint and token bucket**

```python
# market_connector/transport/__init__.py
"""Transport building blocks for exchange connectors."""

from market_connector.transport.endpoint import Endpoint
from market_connector.transport.token_bucket import TokenBucket

__all__ = ["Endpoint", "TokenBucket"]
```

```python
# market_connector/transport/endpoint.py
"""Per-endpoint rate limit configuration."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Endpoint:
    """Declares a REST endpoint with its rate limit budget.

    Attributes:
        path: URL path (e.g. "/api/v3/orders").
        method: HTTP method (GET, POST, PUT, DELETE).
        weight: How many rate-limit tokens this endpoint consumes per call.
        limit: Maximum tokens available per window.
        window: Window duration in seconds.
    """

    path: str
    method: str
    weight: int = 1
    limit: int = 10
    window: float = 1.0
```

```python
# market_connector/transport/token_bucket.py
"""Async token bucket rate limiter."""

from __future__ import annotations

import time

from market_connector.exceptions import RateLimitError


class TokenBucket:
    """Simple token bucket that refills fully after each window elapses.

    Args:
        rate: Maximum tokens per window.
        window: Window duration in seconds.
    """

    def __init__(self, rate: int, window: float) -> None:
        self._rate = rate
        self._window = window
        self._tokens = rate
        self._last_refill = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed >= self._window:
            self._tokens = self._rate
            self._last_refill = now

    async def acquire(self, weight: int = 1) -> None:
        """Consume tokens or raise RateLimitError if exhausted."""
        self._refill()
        if self._tokens < weight:
            raise RateLimitError(
                f"rate limit exhausted: {self._tokens} tokens remaining, "
                f"need {weight}, refills in {self._window}s"
            )
        self._tokens -= weight
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /home/memento/PycharmProjects/Hummingbot/hummingbot/sub-packages/market-connector && pixi run test -- tests/unit/test_endpoint.py tests/unit/test_token_bucket.py -v`
Expected: all 7 tests PASS

- [ ] **Step 6: Commit**

```bash
git add market_connector/transport/ tests/unit/test_endpoint.py tests/unit/test_token_bucket.py
git commit -m "feat: add Endpoint model and TokenBucket rate limiter"
```

---

## Task 5: RestConnectorBase

**Files:**
- Create: `market_connector/transport/rest_base.py`
- Modify: `market_connector/transport/__init__.py` (add export)
- Test: `tests/unit/test_rest_base.py`

**Note:** Uses `httpx.AsyncClient` as default transport. Add `httpx>=0.27` to dependencies before starting.

- [ ] **Step 1: Add httpx dependency**

Add `httpx>=0.27` to both `pyproject.toml` `[project.dependencies]` and `pixi.toml` `[dependencies]`.
Run: `cd /home/memento/PycharmProjects/Hummingbot/hummingbot/sub-packages/market-connector && pixi install`

- [ ] **Step 2: Write the failing tests**

```python
# tests/unit/test_rest_base.py
"""Tests for RestConnectorBase: rate limiting, retry, auth, error mapping."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from market_connector.exceptions import (
    AuthenticationError,
    ExchangeUnavailableError,
    RateLimitError,
)
from market_connector.transport.endpoint import Endpoint
from market_connector.transport.rest_base import RestConnectorBase


@pytest.fixture
def endpoints() -> dict[str, Endpoint]:
    return {
        "get_book": Endpoint(path="/api/book", method="GET", weight=1, limit=5, window=1.0),
        "place_order": Endpoint(path="/api/orders", method="POST", weight=2, limit=5, window=1.0),
    }


class TestRestConnectorBase:
    @pytest.mark.asyncio
    async def test_successful_request(self, endpoints: dict) -> None:
        client = RestConnectorBase(base_url="https://api.example.com", endpoints=endpoints)
        mock_response = httpx.Response(200, json={"status": "ok"})
        with patch.object(client, "_client") as mock_client:
            mock_client.request = AsyncMock(return_value=mock_response)
            result = await client.request("get_book")
        assert result == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_rate_limit_enforced(self, endpoints: dict) -> None:
        client = RestConnectorBase(base_url="https://api.example.com", endpoints=endpoints)
        mock_response = httpx.Response(200, json={})
        with patch.object(client, "_client") as mock_client:
            mock_client.request = AsyncMock(return_value=mock_response)
            # 5 tokens, weight=2 each -> 2 calls OK, 3rd fails
            await client.request("place_order")
            await client.request("place_order")
            with pytest.raises(RateLimitError):
                await client.request("place_order")

    @pytest.mark.asyncio
    async def test_auth_hook_called(self, endpoints: dict) -> None:
        # AsyncMock wraps the sync lambda — it calls it synchronously and returns
        # the result as the awaited value. This works because AsyncMock handles it.
        auth_fn = AsyncMock(side_effect=lambda headers: {**headers, "Authorization": "Bearer tok"})
        client = RestConnectorBase(
            base_url="https://api.example.com", endpoints=endpoints, auth=auth_fn,
        )
        mock_response = httpx.Response(200, json={})
        with patch.object(client, "_client") as mock_client:
            mock_client.request = AsyncMock(return_value=mock_response)
            await client.request("get_book")
            call_kwargs = mock_client.request.call_args
            assert "Authorization" in call_kwargs.kwargs.get("headers", {})

    @pytest.mark.asyncio
    async def test_retry_on_5xx(self, endpoints: dict) -> None:
        client = RestConnectorBase(
            base_url="https://api.example.com",
            endpoints=endpoints,
            max_retries=2,
            retry_delay=0.01,
        )
        fail = httpx.Response(503, json={"error": "unavailable"})
        success = httpx.Response(200, json={"status": "ok"})
        with patch.object(client, "_client") as mock_client:
            mock_client.request = AsyncMock(side_effect=[fail, success])
            result = await client.request("get_book")
        assert result == {"status": "ok"}
        assert mock_client.request.call_count == 2

    @pytest.mark.asyncio
    async def test_5xx_exhausts_retries(self, endpoints: dict) -> None:
        client = RestConnectorBase(
            base_url="https://api.example.com",
            endpoints=endpoints,
            max_retries=2,
            retry_delay=0.01,
        )
        fail = httpx.Response(503, json={"error": "unavailable"})
        with patch.object(client, "_client") as mock_client:
            mock_client.request = AsyncMock(return_value=fail)
            with pytest.raises(ExchangeUnavailableError):
                await client.request("get_book")

    @pytest.mark.asyncio
    async def test_401_raises_auth_error(self, endpoints: dict) -> None:
        client = RestConnectorBase(base_url="https://api.example.com", endpoints=endpoints)
        mock_response = httpx.Response(401, json={"error": "unauthorized"})
        with patch.object(client, "_client") as mock_client:
            mock_client.request = AsyncMock(return_value=mock_response)
            with pytest.raises(AuthenticationError):
                await client.request("get_book")
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd /home/memento/PycharmProjects/Hummingbot/hummingbot/sub-packages/market-connector && pixi run test -- tests/unit/test_rest_base.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 4: Implement RestConnectorBase**

```python
# market_connector/transport/rest_base.py
"""Rate-limited async REST client with retry and auth injection.

Connectors compose this class (not inherit from it). Transport details
(httpx) are encapsulated -- connectors interact via request().
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Awaitable

import httpx

from market_connector.exceptions import (
    AuthenticationError,
    ExchangeUnavailableError,
    RateLimitError,
)
from market_connector.transport.endpoint import Endpoint
from market_connector.transport.token_bucket import TokenBucket

# Type for auth hook: takes headers dict, returns modified headers dict
AuthCallable = Callable[[dict[str, str]], Awaitable[dict[str, str]]]


class RestConnectorBase:
    """Rate-limited REST client with retry and optional auth.

    Args:
        base_url: Base URL for the exchange API.
        endpoints: Mapping of endpoint name -> Endpoint config.
        auth: Async callable that injects auth headers.
        max_retries: Number of retries on transient (5xx) errors.
        retry_delay: Initial delay between retries in seconds (doubles each retry).
    """

    def __init__(
        self,
        base_url: str,
        endpoints: dict[str, Endpoint] | None = None,
        auth: AuthCallable | None = None,
        max_retries: int = 3,
        retry_delay: float = 1.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._endpoints = endpoints or {}
        self._auth = auth
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        self._client = httpx.AsyncClient()
        self._buckets: dict[str, TokenBucket] = {}

    def _get_bucket(self, endpoint: Endpoint) -> TokenBucket:
        key = f"{endpoint.method}:{endpoint.path}"
        if key not in self._buckets:
            self._buckets[key] = TokenBucket(rate=endpoint.limit, window=endpoint.window)
        return self._buckets[key]

    async def request(
        self,
        endpoint_name: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Execute a rate-limited, retried request.

        Raises:
            RateLimitError: Token bucket exhausted.
            AuthenticationError: 401 response.
            ExchangeUnavailableError: 5xx after all retries.
            KeyError: Unknown endpoint_name.
        """
        endpoint = self._endpoints[endpoint_name]
        bucket = self._get_bucket(endpoint)
        await bucket.acquire(weight=endpoint.weight)

        req_headers = dict(headers or {})
        if self._auth is not None:
            req_headers = await self._auth(req_headers)

        url = f"{self._base_url}{endpoint.path}"
        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            response = await self._client.request(
                method=endpoint.method,
                url=url,
                params=params,
                json=data,
                headers=req_headers,
            )

            if response.status_code == 401:
                raise AuthenticationError(f"401 from {endpoint_name}: {response.text}")

            if response.status_code == 429:
                raise RateLimitError(f"429 from {endpoint_name}: exchange-side rate limit")

            if response.status_code >= 500:
                last_error = ExchangeUnavailableError(
                    f"{response.status_code} from {endpoint_name}: {response.text}"
                )
                if attempt < self._max_retries:
                    await asyncio.sleep(self._retry_delay * (2**attempt))
                    continue
                raise last_error

            return response.json()

        raise last_error or ExchangeUnavailableError("request failed")  # pragma: no cover

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()
```

- [ ] **Step 5: Update transport `__init__.py`**

Add to `market_connector/transport/__init__.py`:
```python
from market_connector.transport.rest_base import RestConnectorBase
```
And add `"RestConnectorBase"` to `__all__`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd /home/memento/PycharmProjects/Hummingbot/hummingbot/sub-packages/market-connector && pixi run test -- tests/unit/test_rest_base.py -v`
Expected: all 6 tests PASS

- [ ] **Step 7: Commit**

```bash
git add market_connector/transport/ tests/unit/test_rest_base.py pyproject.toml pixi.toml
git commit -m "feat: add RestConnectorBase with rate limiting, retry, and auth"
```

---

## Task 6: WsConnectorBase

**Files:**
- Create: `market_connector/transport/ws_base.py`
- Modify: `market_connector/transport/__init__.py` (add export)
- Test: `tests/unit/test_ws_base.py`

**Note:** Uses `websockets` library. Add `websockets>=12.0` to dependencies before starting.

- [ ] **Step 1: Add websockets dependency**

Add `websockets>=12.0` to both `pyproject.toml` and `pixi.toml`.
Run: `cd /home/memento/PycharmProjects/Hummingbot/hummingbot/sub-packages/market-connector && pixi install`

- [ ] **Step 2: Write the failing tests**

```python
# tests/unit/test_ws_base.py
"""Tests for WsConnectorBase: connect, subscribe, reconnect, message routing."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_connector.exceptions import GatewayNotStartedError, SubscriptionLimitError
from market_connector.transport.ws_base import Subscription, WsConnectorBase


@pytest.fixture
def ws_client() -> WsConnectorBase:
    return WsConnectorBase(
        url="wss://ws.example.com",
        heartbeat_interval=0.05,
        reconnect_delay=0.01,
        max_reconnect_delay=0.05,
        max_subscriptions=3,
    )


class TestWsConnectorBase:
    @pytest.mark.asyncio
    async def test_not_connected_raises(self, ws_client: WsConnectorBase) -> None:
        with pytest.raises(GatewayNotStartedError):
            await ws_client.send({"type": "subscribe"})

    @pytest.mark.asyncio
    async def test_subscribe_returns_subscription(self, ws_client: WsConnectorBase) -> None:
        callback = MagicMock()
        with patch.object(ws_client, "_ws", create=True):
            ws_client._connected = True
            sub = await ws_client.subscribe("orderbook.BTC-USDT", callback)
        assert isinstance(sub, Subscription)
        assert sub.channel == "orderbook.BTC-USDT"
        assert sub.active

    @pytest.mark.asyncio
    async def test_subscription_limit_enforced(self, ws_client: WsConnectorBase) -> None:
        ws_client._connected = True
        with patch.object(ws_client, "_ws", create=True):
            for i in range(3):
                await ws_client.subscribe(f"channel.{i}", MagicMock())
            with pytest.raises(SubscriptionLimitError):
                await ws_client.subscribe("channel.overflow", MagicMock())

    @pytest.mark.asyncio
    async def test_unsubscribe_frees_slot(self, ws_client: WsConnectorBase) -> None:
        ws_client._connected = True
        with patch.object(ws_client, "_ws", create=True):
            sub = await ws_client.subscribe("channel.0", MagicMock())
            await ws_client.subscribe("channel.1", MagicMock())
            await ws_client.subscribe("channel.2", MagicMock())
            await sub.cancel()
            assert not sub.active
            # Slot freed -- should succeed
            await ws_client.subscribe("channel.3", MagicMock())

    @pytest.mark.asyncio
    async def test_message_routed_to_callback(self, ws_client: WsConnectorBase) -> None:
        callback = MagicMock()
        ws_client._connected = True
        with patch.object(ws_client, "_ws", create=True):
            await ws_client.subscribe("trades.BTC", callback)
        ws_client._route_message("trades.BTC", {"price": "50000"})
        callback.assert_called_once_with({"price": "50000"})
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd /home/memento/PycharmProjects/Hummingbot/hummingbot/sub-packages/market-connector && pixi run test -- tests/unit/test_ws_base.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 4: Implement WsConnectorBase**

```python
# market_connector/transport/ws_base.py
"""Reconnecting async WebSocket client with subscription management.

Connectors compose this class. Subscriptions survive reconnects --
the client automatically re-subscribes after connection recovery.

Note: Connector mixins must wrap Subscription objects in an AsyncContextManager
to satisfy the ExchangeGateway protocol's subscribe_* return types.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from market_connector.exceptions import GatewayNotStartedError, SubscriptionLimitError

logger = logging.getLogger(__name__)

MessageCallback = Callable[[dict[str, Any]], None]


@dataclass
class Subscription:
    """Handle for an active WS subscription."""

    channel: str
    callback: MessageCallback
    active: bool = True
    _owner: WsConnectorBase | None = field(default=None, repr=False)

    async def cancel(self) -> None:
        """Cancel this subscription and free the slot."""
        if self.active and self._owner is not None:
            self._owner._remove_subscription(self)
        self.active = False


class WsConnectorBase:
    """Reconnecting WebSocket client with subscription registry.

    Args:
        url: WebSocket URL.
        auth: Async callable for auth (called on each connect).
        heartbeat_interval: Seconds between heartbeat pings.
        reconnect_delay: Initial reconnect delay in seconds.
        max_reconnect_delay: Maximum reconnect delay (exponential backoff cap).
        max_subscriptions: Maximum concurrent subscriptions (0 = unlimited).
    """

    def __init__(
        self,
        url: str,
        auth: Callable | None = None,
        heartbeat_interval: float = 30.0,
        reconnect_delay: float = 1.0,
        max_reconnect_delay: float = 60.0,
        max_subscriptions: int = 0,
    ) -> None:
        self._url = url
        self._auth = auth
        self._heartbeat_interval = heartbeat_interval
        self._reconnect_delay = reconnect_delay
        self._max_reconnect_delay = max_reconnect_delay
        self._max_subscriptions = max_subscriptions
        self._ws: Any = None
        self._connected = False
        self._subscriptions: dict[str, Subscription] = {}
        self._listen_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None

    async def connect(self) -> None:
        """Connect to the WebSocket server and start listening."""
        import websockets

        self._ws = await websockets.connect(self._url)
        self._connected = True
        self._listen_task = asyncio.create_task(self._listen_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info("WS connected to %s", self._url)

    async def disconnect(self) -> None:
        """Disconnect and cancel all subscriptions."""
        self._connected = False
        for sub in list(self._subscriptions.values()):
            sub.active = False
        self._subscriptions.clear()
        if self._listen_task:
            self._listen_task.cancel()
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self._ws:
            await self._ws.close()
        logger.info("WS disconnected from %s", self._url)

    async def subscribe(self, channel: str, callback: MessageCallback) -> Subscription:
        """Register a subscription. Raises SubscriptionLimitError if cap exceeded."""
        if not self._connected:
            raise GatewayNotStartedError("WebSocket not connected")
        if (
            self._max_subscriptions > 0
            and len(self._subscriptions) >= self._max_subscriptions
        ):
            raise SubscriptionLimitError(
                f"subscription limit reached: {self._max_subscriptions}"
            )
        sub = Subscription(channel=channel, callback=callback, _owner=self)
        self._subscriptions[channel] = sub
        return sub

    def _remove_subscription(self, sub: Subscription) -> None:
        self._subscriptions.pop(sub.channel, None)

    async def send(self, message: dict[str, Any]) -> None:
        """Send a message to the WebSocket server."""
        if not self._connected or self._ws is None:
            raise GatewayNotStartedError("WebSocket not connected")
        import json

        await self._ws.send(json.dumps(message))

    def _route_message(self, channel: str, data: dict[str, Any]) -> None:
        """Route a parsed message to the registered callback."""
        sub = self._subscriptions.get(channel)
        if sub and sub.active:
            sub.callback(data)

    async def _listen_loop(self) -> None:
        """Listen for messages and route them. Reconnects on failure."""
        import json

        delay = self._reconnect_delay
        while self._connected:
            try:
                async for raw in self._ws:
                    msg = json.loads(raw)
                    channel = msg.get("channel", msg.get("type", ""))
                    self._route_message(channel, msg)
            except Exception:
                if not self._connected:
                    break
                logger.warning("WS connection lost, reconnecting in %.1fs", delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._max_reconnect_delay)
                try:
                    await self.connect()
                    delay = self._reconnect_delay
                except Exception:
                    logger.exception("WS reconnect failed")

    async def _heartbeat_loop(self) -> None:
        """Send periodic pings to keep the connection alive."""
        while self._connected:
            await asyncio.sleep(self._heartbeat_interval)
            if self._ws and self._connected:
                try:
                    await self._ws.ping()
                except Exception:
                    pass  # reconnect loop handles recovery
```

- [ ] **Step 5: Update transport `__init__.py`**

Add to `market_connector/transport/__init__.py`:
```python
from market_connector.transport.ws_base import Subscription, WsConnectorBase
```
And add `"WsConnectorBase"`, `"Subscription"` to `__all__`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd /home/memento/PycharmProjects/Hummingbot/hummingbot/sub-packages/market-connector && pixi run test -- tests/unit/test_ws_base.py -v`
Expected: all 5 tests PASS

- [ ] **Step 7: Commit**

```bash
git add market_connector/transport/ tests/unit/test_ws_base.py pyproject.toml pixi.toml
git commit -m "feat: add WsConnectorBase with reconnection and subscription management"
```

---

## Task 7: Contract Test Base

**Files:**
- Create: `market_connector/testing/__init__.py`
- Create: `market_connector/testing/contract.py`
- Create: `market_connector/testing/mock_transport.py`
- Test: `tests/unit/test_contract_base.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_contract_base.py
"""Verify the contract test base works with a mock gateway."""

from __future__ import annotations

from contextlib import asynccontextmanager
from decimal import Decimal
from typing import AsyncIterator

import pytest

from market_connector.primitives import (
    OpenOrder,
    OrderBookSnapshot,
    OrderBookUpdate,
    OrderType,
    TradeEvent,
    TradeType,
)
from market_connector.exceptions import GatewayNotStartedError
from market_connector.protocols import ExchangeGateway
from market_connector.testing.contract import GatewayContractTestBase


class _InMemoryGateway:
    """Minimal in-memory gateway for testing the contract base."""

    def __init__(self) -> None:
        self._started = False
        self._orders: dict[str, OpenOrder] = {}

    def _check_started(self) -> None:
        if not self._started:
            raise GatewayNotStartedError("gateway not started")

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False

    @property
    def ready(self) -> bool:
        return self._started

    async def place_order(self, trading_pair, order_type, side, amount, price):
        self._check_started()
        oid = f"order-{len(self._orders)}"
        self._orders[oid] = OpenOrder(
            client_order_id=oid,
            exchange_order_id=f"ex-{oid}",
            trading_pair=trading_pair,
            order_type=order_type,
            side=side,
            amount=amount,
            price=price or Decimal("0"),
            filled_amount=Decimal("0"),
            status="OPEN",
        )
        return oid

    async def cancel_order(self, trading_pair, client_order_id):
        return self._orders.pop(client_order_id, None) is not None

    async def get_open_orders(self, trading_pair):
        return [o for o in self._orders.values() if o.trading_pair == trading_pair]

    async def get_balance(self, currency):
        return Decimal("10000")

    async def get_orderbook(self, trading_pair):
        return OrderBookSnapshot(trading_pair=trading_pair, bids=[], asks=[], timestamp=0.0)

    async def get_candles(self, trading_pair, interval, limit):
        return []

    async def get_mid_price(self, trading_pair):
        return Decimal("50000")

    async def subscribe_orderbook(self, trading_pair, callback):
        @asynccontextmanager
        async def _ctx() -> AsyncIterator[None]:
            yield
        return _ctx()

    async def subscribe_trades(self, trading_pair, callback):
        @asynccontextmanager
        async def _ctx() -> AsyncIterator[None]:
            yield
        return _ctx()


class TestInMemoryGatewayContract(GatewayContractTestBase):
    """Run the full contract suite against the in-memory mock."""

    @pytest.fixture
    def gateway(self) -> _InMemoryGateway:
        return _InMemoryGateway()

    @pytest.fixture
    def trading_pair(self) -> str:
        return "BTC-USDT"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/memento/PycharmProjects/Hummingbot/hummingbot/sub-packages/market-connector && pixi run test -- tests/unit/test_contract_base.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement contract test base and mock transport**

```python
# market_connector/testing/__init__.py
"""Testing utilities for exchange gateway connectors."""

from market_connector.testing.contract import GatewayContractTestBase
from market_connector.testing.mock_transport import MockRestClient, MockWsClient

__all__ = ["GatewayContractTestBase", "MockRestClient", "MockWsClient"]
```

```python
# market_connector/testing/contract.py
"""Abstract contract test base for gateway conformance.

Connector packages subclass this and provide a gateway fixture.
The contract tests validate that the gateway correctly implements
the ExchangeGateway protocol.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from market_connector.primitives import OpenOrder, OrderBookSnapshot
from market_connector.protocols import ExchangeGateway


class GatewayContractTestBase:
    """Abstract base: subclass and provide `gateway` and `trading_pair` fixtures.

    Each test method validates one aspect of the ExchangeGateway contract.
    Trading pair format validation (BASE-QUOTE canonical) is a per-connector
    responsibility, not tested here.
    """

    @pytest.fixture
    def gateway(self) -> ExchangeGateway:
        raise NotImplementedError("Subclass must provide a gateway fixture")

    @pytest.fixture
    def trading_pair(self) -> str:
        raise NotImplementedError("Subclass must provide a trading_pair fixture")

    # --- Lifecycle ---

    @pytest.mark.asyncio
    async def test_start_sets_ready(self, gateway: ExchangeGateway) -> None:
        assert not gateway.ready
        await gateway.start()
        assert gateway.ready

    @pytest.mark.asyncio
    async def test_stop_clears_ready(self, gateway: ExchangeGateway) -> None:
        """stop() MUST set ready=False per spec Connection Lifecycle section."""
        await gateway.start()
        await gateway.stop()
        assert not gateway.ready

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self, gateway: ExchangeGateway) -> None:
        await gateway.start()
        await gateway.stop()
        await gateway.stop()  # should not raise

    @pytest.mark.asyncio
    async def test_method_before_start_raises(
        self, gateway: ExchangeGateway, trading_pair: str,
    ) -> None:
        """Any gateway method called before start() must raise GatewayNotStartedError."""
        from market_connector.exceptions import GatewayNotStartedError

        with pytest.raises(GatewayNotStartedError):
            await gateway.place_order(trading_pair, "LIMIT", "BUY", Decimal("1"), Decimal("50000"))

    # --- Execution ---

    @pytest.mark.asyncio
    async def test_place_order_returns_client_id(
        self, gateway: ExchangeGateway, trading_pair: str,
    ) -> None:
        await gateway.start()
        order_id = await gateway.place_order(
            trading_pair, "LIMIT", "BUY", Decimal("1.0"), Decimal("50000"),
        )
        assert isinstance(order_id, str)
        assert len(order_id) > 0

    @pytest.mark.asyncio
    async def test_cancel_order_returns_bool(
        self, gateway: ExchangeGateway, trading_pair: str,
    ) -> None:
        await gateway.start()
        order_id = await gateway.place_order(
            trading_pair, "LIMIT", "BUY", Decimal("1.0"), Decimal("50000"),
        )
        result = await gateway.cancel_order(trading_pair, order_id)
        assert isinstance(result, bool)

    @pytest.mark.asyncio
    async def test_get_open_orders_returns_list(
        self, gateway: ExchangeGateway, trading_pair: str,
    ) -> None:
        await gateway.start()
        orders = await gateway.get_open_orders(trading_pair)
        assert isinstance(orders, list)

    @pytest.mark.asyncio
    async def test_get_balance_returns_decimal(
        self, gateway: ExchangeGateway,
    ) -> None:
        await gateway.start()
        balance = await gateway.get_balance("USDT")
        assert isinstance(balance, Decimal)

    # --- Market Data ---

    @pytest.mark.asyncio
    async def test_get_orderbook_returns_snapshot(
        self, gateway: ExchangeGateway, trading_pair: str,
    ) -> None:
        await gateway.start()
        book = await gateway.get_orderbook(trading_pair)
        assert isinstance(book, OrderBookSnapshot)
        assert book.trading_pair == trading_pair

    @pytest.mark.asyncio
    async def test_get_mid_price_returns_decimal(
        self, gateway: ExchangeGateway, trading_pair: str,
    ) -> None:
        await gateway.start()
        price = await gateway.get_mid_price(trading_pair)
        assert isinstance(price, Decimal)
        assert price > 0

    @pytest.mark.asyncio
    async def test_get_candles_returns_list(
        self, gateway: ExchangeGateway, trading_pair: str,
    ) -> None:
        await gateway.start()
        candles = await gateway.get_candles(trading_pair, "1m", 10)
        assert isinstance(candles, list)
```

```python
# market_connector/testing/mock_transport.py
"""Mock transport clients for connector component testing (Tier 2).

Connector packages use these to test their mixins without real HTTP/WS.
"""

from __future__ import annotations

from typing import Any


class MockRestClient:
    """Mock REST client matching RestConnectorBase.request() interface.

    Keyed by endpoint name (same as RestConnectorBase), not method+path.

    Usage:
        mock = MockRestClient()
        mock.register("get_book", {"bids": [], "asks": []})
        result = await mock.request("get_book")
    """

    def __init__(self) -> None:
        self._responses: dict[str, Any] = {}

    def register(self, endpoint_name: str, response: dict[str, Any]) -> None:
        self._responses[endpoint_name] = response

    async def request(
        self,
        endpoint_name: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if endpoint_name not in self._responses:
            raise KeyError(f"No mock registered for endpoint '{endpoint_name}'")
        return self._responses[endpoint_name]


class MockWsClient:
    """Mock WebSocket client for testing subscription-based logic.

    Usage:
        mock = MockWsClient()
        mock.enqueue({"channel": "trades", "data": {...}})
        async for msg in mock:
            process(msg)
    """

    def __init__(self) -> None:
        self._messages: list[str] = []
        self._index = 0

    def enqueue(self, message: dict[str, Any]) -> None:
        import json

        self._messages.append(json.dumps(message))

    def __aiter__(self) -> MockWsClient:
        return self

    async def __anext__(self) -> str:
        if self._index >= len(self._messages):
            raise StopAsyncIteration
        msg = self._messages[self._index]
        self._index += 1
        return msg

    async def close(self) -> None:
        pass

    async def ping(self) -> None:
        pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/memento/PycharmProjects/Hummingbot/hummingbot/sub-packages/market-connector && pixi run test -- tests/unit/test_contract_base.py -v`
Expected: all 11 contract tests PASS (4 lifecycle + 4 execution + 3 market data)

- [ ] **Step 5: Commit**

```bash
git add market_connector/testing/ tests/unit/test_contract_base.py
git commit -m "feat: add GatewayContractTestBase and mock transport utilities"
```

---

## Task 8: hb_compat Bridge

**Files:**
- Create: `market_connector/hb_compat/__init__.py`
- Create: `market_connector/hb_compat/bridge.py`
- Test: `tests/unit/test_hb_compat.py`

**Note:** This module imports from `strategy-framework` (the ONLY module that does).
Check that `hb-strategy-framework` is importable: `pixi run python -c "import strategy_framework"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_hb_compat.py
"""Tests for the hb_compat bridge: sync wrapper around async gateway."""

from __future__ import annotations

import asyncio
import threading
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from market_connector.hb_compat.bridge import LiveMarketAccess
from market_connector.primitives import OrderBookSnapshot


@pytest.fixture
def mock_gateway() -> AsyncMock:
    gw = AsyncMock()
    gw.place_order = AsyncMock(return_value="order-1")
    gw.cancel_order = AsyncMock(return_value=True)
    gw.get_mid_price = AsyncMock(return_value=Decimal("50000"))
    gw.get_balance = AsyncMock(return_value=Decimal("10000"))
    gw.get_orderbook = AsyncMock(
        return_value=OrderBookSnapshot(
            trading_pair="BTC-USDT", bids=[], asks=[], timestamp=0.0,
        )
    )
    return gw


@pytest.fixture
def event_loop_in_thread():
    """Run an event loop in a background thread (simulates hummingbot runtime)."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=2)
    loop.close()


class TestLiveMarketAccess:
    def test_place_order(self, mock_gateway, event_loop_in_thread) -> None:
        bridge = LiveMarketAccess(
            gateway=mock_gateway,
            trading_pair="BTC-USDT",
            loop=event_loop_in_thread,
        )
        result = bridge.place_order("LIMIT", "BUY", Decimal("1"), Decimal("50000"))
        assert result == "order-1"

    def test_cancel_order(self, mock_gateway, event_loop_in_thread) -> None:
        bridge = LiveMarketAccess(
            gateway=mock_gateway,
            trading_pair="BTC-USDT",
            loop=event_loop_in_thread,
        )
        result = bridge.cancel_order("order-1")
        assert result is True

    def test_get_mid_price(self, mock_gateway, event_loop_in_thread) -> None:
        bridge = LiveMarketAccess(
            gateway=mock_gateway,
            trading_pair="BTC-USDT",
            loop=event_loop_in_thread,
        )
        price = bridge.get_mid_price()
        assert price == Decimal("50000")

    def test_timeout_raises(self, event_loop_in_thread) -> None:
        async def slow_op(*a, **kw):
            await asyncio.sleep(10)
            return "too late"

        gw = AsyncMock()
        gw.get_mid_price = slow_op
        bridge = LiveMarketAccess(
            gateway=gw,
            trading_pair="BTC-USDT",
            loop=event_loop_in_thread,
            timeout=0.05,
        )
        with pytest.raises(TimeoutError):
            bridge.get_mid_price()

    def test_get_available_balance(self, mock_gateway, event_loop_in_thread) -> None:
        bridge = LiveMarketAccess(
            gateway=mock_gateway,
            trading_pair="BTC-USDT",
            loop=event_loop_in_thread,
        )
        balance = bridge.get_available_balance("USDT")
        assert balance == Decimal("10000")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/memento/PycharmProjects/Hummingbot/hummingbot/sub-packages/market-connector && pixi run test -- tests/unit/test_hb_compat.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement the bridge**

```python
# market_connector/hb_compat/__init__.py
"""Bridge module: adapts ExchangeGateway to strategy-framework protocols."""

from market_connector.hb_compat.bridge import LiveMarketAccess

__all__ = ["LiveMarketAccess"]
```

```python
# market_connector/hb_compat/bridge.py
"""Sync wrapper adapting ExchangeGateway -> strategy-framework protocols.

This is the ONLY module in hb-market-connector that imports strategy-framework.
Uses run_coroutine_threadsafe because the event loop is always running
in the hummingbot process.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FuturesTimeoutError
from decimal import Decimal

from market_connector.protocols import ExchangeGateway


class LiveMarketAccess:
    """Adapts async ExchangeGateway to sync MarketAccessProtocol.

    Satisfies strategy_framework.protocols.market.MarketAccessProtocol
    and strategy_framework.protocols.market_data.MarketDataProtocol.

    Args:
        gateway: The async exchange gateway to wrap.
        trading_pair: Default trading pair for single-pair methods.
        loop: The running event loop (from the hummingbot process).
        timeout: Maximum seconds to wait for each async call.
    """

    DEFAULT_TIMEOUT: float = 30.0

    def __init__(
        self,
        gateway: ExchangeGateway,
        trading_pair: str,
        loop: asyncio.AbstractEventLoop,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._gateway = gateway
        self._trading_pair = trading_pair
        self._loop = loop
        self._timeout = timeout

    def _run(self, coro) -> object:
        """Submit coroutine to the running loop and block for the result."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=self._timeout)
        except FuturesTimeoutError:
            future.cancel()
            raise TimeoutError(
                f"Gateway call timed out after {self._timeout}s"
            ) from None

    # --- MarketAccessProtocol ---

    def place_order(self, order_type: str, side: str, amount: Decimal, price: Decimal) -> str:
        return self._run(
            self._gateway.place_order(self._trading_pair, order_type, side, amount, price)
        )

    def cancel_order(self, order_id: str) -> bool:
        return self._run(self._gateway.cancel_order(self._trading_pair, order_id))

    def get_mid_price(self) -> Decimal:
        return self._run(self._gateway.get_mid_price(self._trading_pair))

    def get_available_balance(self, currency: str) -> Decimal:
        return self._run(self._gateway.get_balance(currency))

    # --- MarketDataProtocol ---

    def get_order_book_snapshot(self, trading_pair: str | None = None):
        pair = trading_pair or self._trading_pair
        return self._run(self._gateway.get_orderbook(pair))

    def get_candles(self, trading_pair: str, interval: str, limit: int) -> list:
        return self._run(self._gateway.get_candles(trading_pair, interval, limit))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/memento/PycharmProjects/Hummingbot/hummingbot/sub-packages/market-connector && pixi run test -- tests/unit/test_hb_compat.py -v`
Expected: all 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add market_connector/hb_compat/ tests/unit/test_hb_compat.py
git commit -m "feat: add hb_compat bridge (ExchangeGateway -> MarketAccessProtocol)"
```

---

## Task 9: Package Exports and Integration

**Files:**
- Modify: `market_connector/__init__.py`
- Remove: `tests/unit/test_placeholder.py`
- Modify: `pyproject.toml` (optional dependency group for hb_compat)

- [ ] **Step 1: Update package `__init__.py`**

```python
# market_connector/__init__.py
"""hb-market-connector: Exchange Gateway Framework.

Public API:
    - Protocols: ExchangeGateway, ExecutionGateway, MarketDataGateway
    - Primitives: OpenOrder, TradeEvent, OrderBookSnapshot, OrderBookUpdate,
                  OrderType, TradeType
    - Exceptions: GatewayError and subclasses
    - Transport: RestConnectorBase, WsConnectorBase, Endpoint, TokenBucket
    - Testing: GatewayContractTestBase, MockRestClient, MockWsClient
    - Bridge: LiveMarketAccess (requires hb-strategy-framework)
"""

from market_connector.__about__ import __version__
from market_connector.exceptions import (
    AuthenticationError,
    ExchangeUnavailableError,
    GatewayError,
    GatewayNotStartedError,
    OrderNotFoundError,
    OrderRejectedError,
    RateLimitError,
    SubscriptionLimitError,
)
from market_connector.primitives import (
    OpenOrder,
    OrderBookSnapshot,
    OrderBookUpdate,
    OrderType,
    TradeEvent,
    TradeType,
)
from market_connector.protocols import ExchangeGateway, ExecutionGateway, MarketDataGateway

__all__ = [
    "__version__",
    # Protocols
    "ExchangeGateway",
    "ExecutionGateway",
    "MarketDataGateway",
    # Primitives
    "OpenOrder",
    "OrderBookSnapshot",
    "OrderBookUpdate",
    "OrderType",
    "TradeEvent",
    "TradeType",
    # Exceptions
    "AuthenticationError",
    "ExchangeUnavailableError",
    "GatewayError",
    "GatewayNotStartedError",
    "OrderNotFoundError",
    "OrderRejectedError",
    "RateLimitError",
    "SubscriptionLimitError",
]
```

- [ ] **Step 2: Remove the placeholder test**

Delete `tests/unit/test_placeholder.py`.

- [ ] **Step 3: Add optional dependency group for hb_compat**

In `pyproject.toml`, add an optional dependency group so the core framework
can be installed without strategy-framework:

```toml
[project.optional-dependencies]
bridge = ["hb-strategy-framework>=0.1.0"]
```

Move `hb-strategy-framework` from `[project.dependencies]` to the optional group.
The final state of dependencies should be:

```toml
[project]
dependencies = [
    "pydantic>=2",
    "httpx>=0.27",
    "websockets>=12.0",
]

[project.optional-dependencies]
bridge = ["hb-strategy-framework>=0.1.0"]
```

- [ ] **Step 4: Verify `typecheck` task exists**

Check `pixi.toml` for a `typecheck` task. If missing, add:
```toml
[feature.quality.tasks]
typecheck = "mypy market_connector"
```

- [ ] **Step 5: Run the full test suite**

Run: `cd /home/memento/PycharmProjects/Hummingbot/hummingbot/sub-packages/market-connector && pixi run test -v`
Expected: all tests PASS across all modules

- [ ] **Step 6: Run quality gates**

Run: `cd /home/memento/PycharmProjects/Hummingbot/hummingbot/sub-packages/market-connector && pixi run lint && pixi run format-check && pixi run typecheck`
Expected: all pass. Fix any violations before committing.

- [ ] **Step 7: Commit**

```bash
git add market_connector/__init__.py pyproject.toml
git rm tests/unit/test_placeholder.py
git commit -m "feat: wire up package exports and make strategy-framework optional"
```

---

## Post-Implementation Checklist

After all tasks are complete:

- [ ] Full test suite passes: `pixi run test`
- [ ] Lint passes: `pixi run lint`
- [ ] Format passes: `pixi run format-check`
- [ ] Type check passes: `pixi run typecheck`
- [ ] Coverage meets 90% threshold: `pixi run test -- --cov --cov-report=term-missing`
- [ ] All protocol types are `@runtime_checkable`
- [ ] All primitives are `frozen=True`
- [ ] No `strategy-framework` imports outside `hb_compat/`
- [ ] `GatewayContractTestBase` is importable from `market_connector.testing`

## Next Plan

After this framework is validated, create a separate implementation plan for
`hb-coinbase-connector` -- the reference connector that exercises all framework
components with real exchange schemas, converters, and fixtures.
