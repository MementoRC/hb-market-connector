# Exchange Gateway Framework Design

**Date:** 2026-04-23
**Status:** Approved
**Scope:** `hb-market-connector` (framework) + `hb-coinbase-connector` (reference implementation)
**Inspired by:** [hummingbot/hummingbot#6307](https://github.com/hummingbot/hummingbot/pull/6307)

---

## Motivation

Hummingbot connectors currently mix transport, auth, data type conversion, and business logic
in a single monolithic class. Consequences:

- Changing auth or reconnection logic breaks order management
- Testing order handling requires a live (or HTTP-mocked) exchange connection
- Each new connector copy-pastes the same infrastructure with slight variations
- No stable interface allows swapping connectors in strategy code

This design separates those concerns into four independent layers, each testable in isolation,
and formalises a protocol that any exchange implementation must satisfy.

---

## Design Goals

1. **Transport isolation** — auth, rate limiting, and WebSocket reconnection never touch business logic
2. **Maximum testability** — unit-test schemas and converters from JSON fixtures; test logic with mock transport; no live exchange needed for any non-integration test
3. **Pluggable connectors** — each exchange is its own sub-package; users install only what they need
4. **Protocol stability** — consumer code (strategies, scripts) depends on `ExchangeGateway`, not on any concrete connector
5. **Framework agnostic** — `hb-market-connector` does not import from `strategy-framework`; the bridge is one-directional

---

## Package Topology

```
strategy-framework          (protocols + primitives consumers use)
        ↑
hb-market-connector         (ExchangeGateway framework — THIS PACKAGE)
        ↑              ↑
hb-coinbase-connector  hb-binance-connector  hb-kraken-connector  ...
(reference impl)       (future)
```

Each exchange connector sub-package:
- Has its own `pyproject.toml`, `pixi.toml`, CI pipeline, and test suite
- Declares `hb-market-connector` as its only framework dependency
- Is independently versioned and installable

Users install only the exchanges they need:
```toml
dependencies = ["hb-market-connector>=1.0", "hb-coinbase-connector>=1.0"]
```

---

## Four-Layer Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│ Consumer Layer                                                       │
│ Uses MarketAccessProtocol / MarketDataProtocol (strategy-framework)  │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ via hb_compat bridge
┌──────────────────────────────▼──────────────────────────────────────┐
│ Gateway Protocol Layer (hb-market-connector)                         │
│ ExecutionGateway + MarketDataGateway + ExchangeGateway               │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ implemented by
┌──────────────────────────────▼──────────────────────────────────────┐
│ Adapter Layer (per-connector — e.g. hb-coinbase-connector)           │
│ CoinbaseGateway composed of domain mixins                            │
│ Transport injected, never constructed inside business logic          │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ calls pure functions
┌──────────────────────────────▼──────────────────────────────────────┐
│ Conversion Layer (per-connector)                                     │
│ converters.py — pure functions: exchange schema → domain primitive   │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ parses
┌──────────────────────────────▼──────────────────────────────────────┐
│ Schema Layer (per-connector)                                         │
│ Pydantic models for every exchange REST response and WS message      │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ wraps
┌──────────────────────────────▼──────────────────────────────────────┐
│ Transport Layer (hummingbot RESTAssistant / WSAssistant or custom)   │
│ Untouched — framework sits above this                                │
└─────────────────────────────────────────────────────────────────────┘
```

**Key principle:** each layer imports only from the layer directly below it. The adapter layer
never imports `strategy_framework`. The schema layer never knows about the gateway.
Transport is always injected — never constructed inside business logic.

---

## Gateway Protocols (`hb-market-connector`)

### `ExecutionGateway`

```python
class ExecutionGateway(Protocol):
    async def place_order(
        self,
        trading_pair: str,
        order_type: OrderType,
        side: TradeType,
        amount: Decimal,
        price: Decimal | None,
    ) -> str: ...  # → client_order_id

    async def cancel_order(self, trading_pair: str, client_order_id: str) -> bool: ...
    async def get_open_orders(self, trading_pair: str) -> list[OpenOrder]: ...
    async def get_balance(self, currency: str) -> Decimal: ...
```

### `MarketDataGateway`

```python
class MarketDataGateway(Protocol):
    async def get_orderbook(self, trading_pair: str) -> OrderBookSnapshot: ...
    async def get_candles(self, trading_pair: str, interval: str, limit: int) -> list[CandleData]: ...
    async def get_mid_price(self, trading_pair: str) -> Decimal: ...

    # Subscriptions: callback-based, WS lifecycle internal to the implementation
    async def subscribe_orderbook(
        self, trading_pair: str, callback: Callable[[OrderBookUpdate], None],
    ) -> AsyncContextManager: ...

    async def subscribe_trades(
        self, trading_pair: str, callback: Callable[[TradeEvent], None],
    ) -> AsyncContextManager: ...
```

### `ExchangeGateway`

```python
class ExchangeGateway(ExecutionGateway, MarketDataGateway, Protocol): ...
```

Consumers that need only data use `MarketDataGateway`. Consumers that need execution use
`ExecutionGateway`. The composite `ExchangeGateway` is what connector implementations satisfy.

---

## New Domain Primitives (`hb-market-connector`)

These complement `CandleData` and `OrderBookSnapshot` (re-used from `strategy-framework`):

| Primitive | Fields |
|-----------|--------|
| `OpenOrder` | `client_order_id`, `exchange_order_id`, `trading_pair`, `order_type`, `side`, `amount`, `price`, `filled_amount`, `status` |
| `TradeEvent` | `exchange_trade_id`, `trading_pair`, `price`, `amount`, `side`, `timestamp` |
| `OrderBookUpdate` | `trading_pair`, `bids: list[tuple[Decimal, Decimal]]`, `asks: list[tuple[Decimal, Decimal]]`, `is_snapshot: bool` |

All frozen Pydantic v2 models.

---

## Framework Base Classes (`hb-market-connector`)

`RestConnectorBase` and `WsConnectorBase` are **optional building blocks**, not mandatory
base classes. Each connector uses what fits. They provide:

- `RestConnectorBase`: rate-limit token bucket, retry with exponential backoff, auth hook
- `WsConnectorBase`: reconnect loop, heartbeat, message queue, subscription registry

Auth is **per-connector** — schemes vary too widely (API keys, JWT, OAuth, HMAC) to abstract
usefully at the framework level. The auth hook in `RestConnectorBase` accepts a callable:
```python
base = RestConnectorBase(auth=CoinbaseAuth(api_key, secret))
```

---

## Per-Connector File Structure (`hb-coinbase-connector` as reference)

```
coinbase_connector/
├── schemas/
│   ├── rest.py          # Pydantic models: PlaceOrderResponse, FillEvent, …
│   └── ws.py            # Pydantic models: L2UpdateMessage, TradeMessage, …
├── converters.py        # Pure functions: schema → OpenOrder, TradeEvent, …
├── mixins/
│   ├── orders.py        # Place/cancel/query — uses injected REST client
│   ├── market_data.py   # Orderbook/candles — uses injected REST + WS clients
│   └── accounts.py      # Balance/trading rules — uses injected REST client
├── coinbase_gateway.py  # CoinbaseGateway(ExchangeGateway) — thin composition
├── auth.py              # Coinbase JWT/API key signing
└── endpoints.py         # URL constants + per-endpoint rate limit specs
```

### `schemas/rest.py` example

```python
class PlaceOrderResponse(BaseModel):
    order_id: str
    client_order_id: str
    status: Literal["OPEN", "FILLED", "CANCELLED"]

class FillEvent(BaseModel):
    order_id: str
    trade_id: str
    price: str        # exchange sends strings — converters handle Decimal coercion
    size: str
    side: Literal["BUY", "SELL"]
    trade_time: datetime
```

### `converters.py` example

```python
def fill_to_trade_event(fill: FillEvent) -> TradeEvent:
    return TradeEvent(
        price=Decimal(fill.price),
        amount=Decimal(fill.size),
        side=TradeType.BUY if fill.side == "BUY" else TradeType.SELL,
        timestamp=fill.trade_time,
    )
```

### `mixins/orders.py` example

```python
class CoinbaseOrdersMixin:
    _rest: RestClient  # injected in CoinbaseGateway.__init__

    async def place_order(self, trading_pair, order_type, side, amount, price):
        raw = await self._rest.post(Endpoints.PLACE_ORDER, payload={...})
        response = PlaceOrderResponse.model_validate(raw)
        return response.client_order_id
```

### `coinbase_gateway.py`

```python
class CoinbaseGateway(CoinbaseOrdersMixin, CoinbaseMarketDataMixin, CoinbaseAccountsMixin):
    def __init__(self, rest: RestClient, ws: WsClient) -> None:
        self._rest = rest
        self._ws = ws
```

No logic in the gateway. It is purely a composition root.

---

## Testing Strategy

Three tiers, no live exchange required for the first two:

### Tier 1 — Unit (zero I/O)

- **`test_schemas.py`**: `PlaceOrderResponse.model_validate(json.load("fixtures/rest/place_order.json"))` — validates every schema field against real captured responses
- **`test_converters.py`**: pure function assertions, parameterized over fixture files
- Coverage target: 100% of converters; every schema model exercised

### Tier 2 — Component (mock transport)

- **`test_orders_mixin.py`**, **`test_market_data_mixin.py`**: inject `MockRestClient` / `MockWsClient` returning fixture dicts
- Tests business logic only — no HTTP
- Coverage target: all paths in every mixin

### Tier 3 — Contract (gateway conformance)

- Shared test suite in `hb-market-connector/tests/contract/`
- Parameterized against the `ExchangeGateway` protocol
- Run against each connector's gateway using mock transport + fixtures
- Validates: "does this connector correctly satisfy the gateway protocol?"

### Fixture Convention

```
tests/
  fixtures/
    rest/
      place_order_success.json
      place_order_insufficient_funds.json
      get_orderbook.json
    ws/
      l2_update.json
      trade.json
```

Real exchange JSON captured once from the live API, reused forever. Schema tests break
immediately when the exchange silently changes its response format.

---

## `hb_compat` Bridge

Lives in `hb-market-connector`, not `strategy-framework`. Adapts the async `ExchangeGateway`
to the sync `MarketAccessProtocol` / `MarketDataProtocol` that strategy-framework consumers expect:

```python
class LiveMarketAccess:
    """Adapts ExchangeGateway → MarketAccessProtocol for strategy-framework consumers."""

    def __init__(self, gateway: ExchangeGateway, trading_pair: str, loop: asyncio.AbstractEventLoop) -> None:
        self._gateway = gateway
        self._trading_pair = trading_pair
        self._loop = loop

    def place_order(self, order_type, side, amount, price) -> str:
        return self._loop.run_until_complete(
            self._gateway.place_order(self._trading_pair, order_type, side, amount, price)
        )
```

---

## Future Extensibility

- **CCXT connector**: `hb-ccxt-connector` implements `ExchangeGateway` using CCXT as transport — no framework changes needed
- **Additional exchanges**: copy the connector file structure, implement the schemas + converters + mixins for the new exchange
- **Upstream contribution**: once the pattern is validated through the reference implementation, the abstraction can be proposed to hummingbot upstream

---

## Out of Scope

- Order book state management (a strategy concern, not a connector concern)
- Position tracking (belongs in executor layer)
- Paper trading / simulation (belongs in `hb-market-simulator`)
