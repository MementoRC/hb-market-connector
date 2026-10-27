# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Interactive Brokers: `IbGatewayTransport.subscribe(..., on_error=)` and `SubscriptionHandle.error` / `SubscriptionHandle.closed`. Error 101 or a hard permission code on a trades (tick-by-tick) stream frees its line once, closes the handle, sets `error` and calls `on_error`. Other channels accept `on_error` but don't fire it yet (#129).
- `RestConnectorBase.request(..., pools=)`: hb-rate-limit's per-call pool override (0 skips a linked pool; other registered pools are charged ad hoc) (#130, #131).

### Changed

- Kraken: cancels of orders aged 300s or more reserve the declared per-pair endpoint limit with a zero matching-pool charge, instead of going unkeyed through a lazily created default limit (#131).
- Kraken: `cancel_orders_batch` charges every pair's matching pool through one ad-hoc multi-pool reserve; no limit is registered per pair combination (#130).
- Requires hb-rate-limit v0.5.0.

### Removed

- **BREAKING:** `rate_limits.limiter.ensure_multi_keyed_limit` (added in 0.3.0). Use `RestConnectorBase.request(..., pools=)` instead (#130).
>
## [0.3.0] - 2026-10-26

### Added

- `RestConnectorBase.request(..., weights=)`: optional per-call override of the rate-limit weights reserved for that request, including linked pools (#116).
- `rate_limits.limiter.ensure_multi_keyed_limit`: one limit linked to several keyed pools, for requests that charge more than one pair (#117).
- Interactive Brokers: `IbConnectionSpec.market_data_lines` (default 100) caps concurrent market-data lines per connection; a new stream over the cap raises `MarketDataLinesExhaustedError` (a `SubscriptionLimitError`) before the request is sent, and IB error 101 maps to it. `IbGatewayTransport.market_data_lines_in_use` reports usage (#104).
- Kraken: cancel, amend and edit charge Kraken's order-age penalties on the per-pair matching counter (`exchanges.kraken.age_penalties`) (#116).
- Kraken: `amend_order`, `edit_order`, `place_orders_batch` and `cancel_orders_batch` on the orders mixin, with matching-counter charges (#117).
- Kraken: per-pair open-order cap by tier (`specs.KRAKEN_MAX_OPEN_ORDERS_PER_PAIR`: 60/80/225), enforced before `AddOrder`; `EOrder:Orders limit exceeded` maps to `errors.OpenOrdersLimitError` (an `OrderRejectedError`) (#118).

### Changed

- Interactive Brokers: the 80% market-data warning is now per connection and measured against `market_data_lines`; the process-global subscription counter is removed (#104).
- Coinbase: the `User-Agent` header reports the package version from `__about__`.

### Fixed

- Kraken: `place_order` raises the mapped Kraken error on an error reply instead of failing response validation (#118).
- Interactive Brokers: a failed `subscribe()` no longer leaks its event-bus subscription (#104).

## [0.2.0] - 2026-10-26

### Removed (breaking)

- `FlatRateLimit` and `TieredRateLimit` removed from `market_connector.rate_limits`, along with the `RateLimit` acquire protocol and the `rate_limits.flat` / `tiered` / `pool` modules — replaced by hb-rate-limit's `RateLimiter` via `limiter_from_flat_spec` / `limiter_from_tiered_spec` (#108).
- `TokenBucket` removed from `market_connector.transport`, along with `transport.token_bucket` — `RestConnectorBase` uses hb-rate-limit (#108).

### Changed (breaking)

- `testing.RateLimitConformance(make, *, limit, interval)` now delegates to `rate_limit.testing.assert_conforms_rest_limiter`; the acquire-based constructor is gone.
- Spec dataclasses (`PoolSpec`, `FlatRateLimitSpec`, `TierProfile`, `TieredRateLimitSpec`) live in `market_connector.rate_limits.specs`; still re-exported from `market_connector.rate_limits`.

### Added

- `ensure_keyed_limit` exported from `market_connector.rate_limits`.
