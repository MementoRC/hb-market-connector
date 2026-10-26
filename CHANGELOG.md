# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.2.0] - 2026-10-26

### Removed (breaking)

- `FlatRateLimit` and `TieredRateLimit` removed from `market_connector.rate_limits`, along with the `RateLimit` acquire protocol and the `rate_limits.flat` / `tiered` / `pool` modules — replaced by hb-rate-limit's `RateLimiter` via `limiter_from_flat_spec` / `limiter_from_tiered_spec` (#108).
- `TokenBucket` removed from `market_connector.transport`, along with `transport.token_bucket` — `RestConnectorBase` uses hb-rate-limit (#108).

### Changed (breaking)

- `testing.RateLimitConformance(make, *, limit, interval)` now delegates to `rate_limit.testing.assert_conforms_rest_limiter`; the acquire-based constructor is gone.
- Spec dataclasses (`PoolSpec`, `FlatRateLimitSpec`, `TierProfile`, `TieredRateLimitSpec`) live in `market_connector.rate_limits.specs`; still re-exported from `market_connector.rate_limits`.

### Added

- `ensure_keyed_limit` exported from `market_connector.rate_limits`.
