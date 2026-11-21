# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.11.0] - 2026-11-20

### Added

- `LegSnapshot` and `SupportsLegLookup` (`get_leg`, `list_open_legs`) protocols, implemented by `KrakenGateway` via `LegLookupMixin` (#250)
- hb_compat exchange registry (`list_exchanges` / `get_exchange`), `ExchangeDescriptor` with Kraken descriptor, `ConnectorCoreHandle` lifecycle and `ConnectorCoreProtocol`; Kraken core exposes dtp `TradingRules` / `OrderBookSnapshot` and price/size quanta (#265)
- `KrakenConnectorCore.buy/sell` and `KrakenConnectorBridge.place_order` accept `trigger: TriggerSpec | None`, forwarded to the gateway with the same validation and argument convention as `KrakenGateway.place_order`; invalid arguments raise synchronously before anything is tracked or sent (#288)
- Kraken `TriggerSpec`, native bracket via conditional close, `userref` link codec (#240)
- `KrakenConnectorCore.start()` is self-driving (#266)
- `max_group_id` (read-only) on bounded link codecs; unbounded codecs lack it (#291)

### Changed

- `KrakenConnectorCore.buy/sell` and `KrakenConnectorBridge.place_order` now emit the gateway's `DeprecationWarning` (removal in #241) for a triggered order type placed without `trigger=`; this reverses the #240 hb_compat silence. Request bodies and events are unchanged (#288)
- `KrakenConnectorCore` floors absolute trigger values, absolute trail distances and `limit_price` (legacy and `trigger=` forms) to the pair's price tick; under `trigger=` the created event reports the quantized trigger, matching the legacy form; `trigger` added to `ConnectorCoreProtocol.buy/sell` (#288 / PR #294 review)
- `KrakenConnectorCore.trading_rules` now holds dtp `TradingRules` and `get_order_book` returns dtp `OrderBookSnapshot` (#265)

- **BREAKING:** `HBOrder.stop_price` is removed (deprecated since 0.9.0); use `trigger=TriggerSpec(kind=TriggerKind.ABSOLUTE, value=...)`. `HBOrder` now rejects unknown fields (`extra="forbid"`) (#241).

### Fixed

- Kraken spot rejects `sandbox=True` with `ValueError` (Kraken spot has no sandbox; REST previously pointed at demo-futures while WS stayed on production) (#260)
- Kraken converter maps `TAKE_PROFIT_LIMIT` and `TRAILING_STOP_LIMIT` (#261)
- Age+count finished-order retention; late trades are surfaced for evicted orders (#284)
- Kraken `QueryOrders` requests `trades=true`, `QueryTrades` is chunked, and terminal orders are held until fills are reconciled (#286)
- EventKey int event-id contract, requires hb-event-bus>=0.4 (#262)
- Kraken single-owner WS reconnect, with `is_connected` and a connection generation (#273)
- Kraken private WS is wired, with ack-gated private subscribes and private-frame decode (#274)

## [0.10.0] - 2026-11-16

### Added

- `SupportsBracket` / `SupportsOco` protocols and read-only `BracketHandle` / `OcoHandle` handle protocols (#239); `check_linked_protocol_capabilities` contract helper.
- `cancel_linked(handle) -> LinkedCancelOutcome` on `SupportsBracket` / `SupportsOco` (#251); IB cancels each placed leg natively and reports per-leg outcomes, signed residual fill and status (dtp v0.13.0). Coinbase unchanged.
- IB: `place_oco`, and `place_bracket(target, spec)` taking a `LinkedOrderSpec` (single-exit brackets supported). `IbBracket` / `IbOcaGroup` expose the handle members.
- Per-exchange `LinkedOrderIdCodec` (Kraken `cl_ord_id`, Coinbase `client_order_id`) (#248)
- Kraken `cl_ord_id` primitives for emulated linked legs (#255)
- `KrakenConnectorCore.buy/sell` keyword-only `limit_price`, `price_in_percent`, `position_action`, `expiration_ts`; stop/take-profit/trailing `*_LIMIT` orders require `limit_price` (ValueError); unknown kwargs raise TypeError (#259)

### Deprecated

- IB `place_bracket(ref, entry, take_profit, stop_loss)` per-leg form; pass a `LinkedOrderSpec`.

### Changed

- Bump `hb-data-type-primitives` to v0.11.0 (LinkedOrderSpec/LegSpec/LinkKind/LegRole for #239).
- Bump `hb-data-type-primitives` to v0.12.0 (CancelOutcome/LinkedCancelOutcome for #251).
- Bump `hb-data-type-primitives` to v0.13.0 (`residual_filled` is signed net exposure, for #251).

## [0.9.0] - 2026-11-15

### Added

- `UnsupportedOrderError(GatewayError)` (exported from `market_connector`) with `exchange`, `order_type`, `feature`, `reason` attributes, for well-formed orders a venue cannot honour (#236).
- `HBOrder.trigger: TriggerSpec | None` for conditional order types (STOP_LOSS, STOP_LOSS_LIMIT); IB builds `auxPrice` from an ABSOLUTE/LAST trigger and raises `UnsupportedOrderError` for other kinds/references (#237).
- `OrderCapabilities` (frozen descriptor), `LinkedMode` and the `HasCapabilities` protocol (exported from `market_connector`); Coinbase, Kraken and IB gateways expose `order_capabilities` (#238).
- `market_connector.testing.contract.check_order_capabilities` and `OrderCapabilitiesContractBase` to verify a descriptor against the adapter's `UnsupportedOrderError` behaviour (#238).

### Changed

- Kraken `place_order` raises `UnsupportedOrderError` (was `ValueError`) for valid canonical order types it does not map (e.g. `AMM_SWAP`); unknown strings still raise `ValueError` (#238).
- `place_order` on the hb_compat bridge `LiveMarketAccess` and `live_market_access.LiveMarketAccess` is now typed `OrderType | str` / `TradeType | str` on the `data_type_primitives.common` enums; passing `str` is deprecated (emits `DeprecationWarning`, removal planned). Fixes `AttributeError` for enum inputs in `live_market_access` (#243).
- Order validation moved from the IB transport to `HBOrder` construction: conditional types require a trigger, a trigger on MARKET/LIMIT/LIMIT_MAKER is rejected, STOP_LOSS_LIMIT requires `price`. `ValueError` now surfaces at construction, not at `_hb_to_ib_order` (#237).
- **BREAKING:** Coinbase `place_order` raises `UnsupportedOrderError` (was `ValueError`) for valid-but-unsupported order types; unknown strings still raise `ValueError`. IB `_hb_to_ib_order` (and `place_order`/`what_if`) raises `UnsupportedOrderError` (was `NotImplementedError`) for `LIMIT_MAKER` and unknown types. IB `place_bracket`/`place_oca` without a contract resolver raise `GatewayNotStartedError` (was `RuntimeError`). Update `except` clauses accordingly (#236).
- `OrderOutcomeUnknownError` now also subclasses `data_type_primitives.OrderOutcomeUnknownError` (dtp >=v0.10.0); pickle round-trip fixed (#234).

### Deprecated

- `HBOrder.stop_price`: use `trigger=TriggerSpec(kind=TriggerKind.ABSOLUTE, value=...)`. It still works, emits `DeprecationWarning`, and is mapped to an ABSOLUTE trigger; setting both raises `ValueError` (#237).

## [0.8.0] - 2026-11-14

### Added

- Coinbase: `place_order` accepts an optional keyword `client_order_id` (else generates `coinbase-<uuid4>`), fixed before the single POST. A lost response (5xx/Cloudflare/transport error) now raises the new `OrderOutcomeUnknownError(ExchangeUnavailableError)` carrying `.client_order_id` instead of a bare error; the POST is never re-sent. New `get_order_by_client_order_id` resolves such an order via `list_orders` (paginated), returning the `OpenOrder` or None; the UNKNOWN/grace policy is left to the order tracker (#221).
- Kraken: the gateway now converts hb pairs (`BTC-USD`) to the AssetPairs `altname` (`XBTUSD`) via a lazily fetched, cached pair map in `get_orderbook`/`get_ticker`/`get_mid_price`/`place_order`; unknown dashed pairs raise `ValueError` before any request, native pairs pass through. `build_pair_tables` moved to `kraken.pairs` (re-exported from `kraken_core_support`) (#213).
- Kraken: `KrakenConnectorCore.status_dict` (symbols_mapping_initialized, trading_rule_initialized, account_balance, status_polling_initialized) and derived read-only `ready` (#214).
- Kraken conditional orders (STOP_LOSS, TAKE_PROFIT and `*_LIMIT` variants) via `place_order`, with `limit_price` and `price_in_percent`; trailing types deferred (#209).
- Kraken `TRAILING_STOP` / `TRAILING_STOP_LIMIT` orders via `place_order`: `price` is the trail distance (`+<d>`, `%` with `price_in_percent`), `limit_price` the signed offset from the trigger (`price2`); validated before any request (#223).
- Kraken `KrakenConnectorCore` (hummingbot-free `ExchangePyBase`-style core: sync `buy`/`sell`/`cancel`, in-flight tracking, REST/WS fill handling, status polling, price/order-book/balance caches, trading rules) and `hb_compat.order_events` (7 `MarketEvent` payload dataclasses plus `MARKET_EVENT_IDS`); gateway gains `get_balances_ex` (BalanceEx), `AssetPairInfo.costmin` and `OrderInfo.trades` (#211).

### Changed

- Bump `hb-data-type-primitives` to v0.8.0; Kraken `KrakenConnectorCore` uses upstream `OrderState.UNKNOWN` (so `InFlightOrder.is_open` is True for UNKNOWN orders) instead of a local enum (Refs #214). v0.8.0 compares fills exactly (`executed >= amount`), no dust tolerance.
- Coinbase place_order now rejects an unknown side with ValueError before HTTP (#231)
- Kraken: an order whose AddOrder response is lost (timeout / transport error) is no longer failed; it is tagged with a unique int32 `userref`, kept as UNKNOWN, and adopted or failed by looking it up in OpenOrders then ClosedOrders. Gateway gains `place_order(userref=)`, `get_open_orders(userref=)` and `get_closed_orders` (#214).
- Kraken: `EService:Market in cancel_only mode` / `post_only mode` now map to the new `ExchangeCancelOnlyError` / `ExchangePostOnlyError` (both `ServiceUnavailableError` subclasses). `KrakenConnectorCore` fails such an order definitively and, after a cancel-only reply, fails new orders locally for an exponentially growing window (`cancel_only_backoff`, `cancel_only_backoff_max`, injectable `clock`) until an AddOrder succeeds; cancels are never blocked (#214).
- Kraken REST retry hardening (#214 item 6): `Endpoint.idempotent` (False for Kraken `add_order`/`add_order_batch`/`edit_order` and Coinbase `place_order`) makes the REST loop send such calls once (5xx/Cloudflare raises `ExchangeUnavailableError`, which `KrakenConnectorCore` treats as UNKNOWN). Idempotent calls now also retry Cloudflare/HTML bodies and httpx transport errors, re-signing each attempt with a fresh nonce, and raise `ExchangeUnavailableError` on exhaustion. `RateLimitError.retry_after`; `EGeneral:Temporary lockout` maps to it (>= 60 s); new `InvalidNonceError` (never retried) with nonce-window / API-key guidance; new `KrakenConnectorCore(unknown_order_grace=)`.

### Fixed

- Coinbase `place_order` and IB `HBOrder` accept canonical `data_type_primitives` `OrderType`/`TradeType` members (and case-insensitive strings), normalised by member name via `hb_compat.primitives.coerce_order_type`/`coerce_trade_type`; Kraken `side` accepts canonical `TradeType` (#231).

## [0.7.0] - 2026-11-13

### Added

- Interactive Brokers: `OrderHandle` exposes public read-only `perm_id` (`int | None`, None until IB assigns a permId) and `client_order_id` (`int`, the `orderId`) accessors, so consumers can match orders by either IB id (#203, #205).

### Changed

- QA: bandit security gate wired into `quality`, plus a pip-audit `safety-check` task (#202).

### Fixed

- Dev env: the `interactive-brokers` pixi env now includes `cryptography` and `pyjwt`, so whole-repo test runs collect the coinbase tests (#204).

## [0.6.0] - 2026-11-13

### Added

- Interactive Brokers: `OrderHandle` exposes public read-only `symbol`, `action` and `total_quantity` (`Decimal`) accessors, so open orders from other clients or manual TWS entry returned by `sync_open_orders()` can be identified without reading private state (#199, #200).

## [0.5.0] - 2026-11-12

### Added

- Interactive Brokers: `IbGatewayGateway.managed_accounts()` and `IbGatewayTransport.managed_accounts()` return the Gateway session's logged-in accounts and raise the new `ManagedAccountsUnavailableError` when IB reports none. `sync_open_orders(timeout)` is an on-demand all-clients open-order refresh that raises `OpenOrdersSyncTimeoutError` or `OpenOrdersSyncError` instead of logging (#188).
- QA: import-linter `lint-boundaries` gate added to enforce package import boundaries (#195).

### Fixed

- `hb_compat.common` sources `OrderType`/`TradeType` from `hb-data-type-primitives`; `market_connector` has no `hummingbot` imports (#194).

## [0.4.0] - 2026-11-12

### Added

- Interactive Brokers: `IB_READONLY=1` makes the live tests and `ib-gateway-compat` connect with `readonly=True` and skips `ib_orders` tests, so the suite runs against a `READ_ONLY_API=yes` Gateway; the `ib-gateway-latest` compose service is back to `READ_ONLY_API=yes` (#154).
- Interactive Brokers: read-only connect handling. `IbConnectionSpec(readonly=True)` skips the all-open-orders sync, and `place_order`, `place_bracket`, `place_oca`, `cancel_order` and `cancel_all` raise the new `IbReadOnlyModeError`. A Gateway with `READ_ONLY_API=yes` reached with `readonly=False` (ib_async's order-sync timeout) now fails the connect fast with `IbReadOnlyApiError` instead of retrying (#151). `what_if` is also refused with `IbReadOnlyModeError` in read-only mode, since IB treats whatIf as an order-API request.
- Interactive Brokers: `pixi run ib-gateway-compat --channel {stable,latest}` records the negotiated server version, ib_async ceiling and read-only live suite result in a README compatibility matrix; new `ib_orders` marker and `test-ib-gateway` task separate order-placing live tests from read-only ones (#144).
- Interactive Brokers: `ib-gateway-compat` auto-detects the Gateway build from its container (`IB_GATEWAY_CONTAINER` / `--container`), reading only `IB_GATEWAY_VERSION`; precedence is `--gateway-build` > `IB_GATEWAY_BUILD` > detection > `unknown` (#148).
- Interactive Brokers: `IbGatewayTransport.server_info` / `IbGatewayGateway.server_info` (`IbServerInfo`: `server_version`, `max_client_version`, `at_client_ceiling`), refreshed on every connect and reconnect and kept across disconnects, plus one INFO log line per connect recording the negotiated server version (#59).
- Interactive Brokers: `IbGatewayTransport.subscribe(..., on_error=)` and `SubscriptionHandle.error` / `SubscriptionHandle.closed`. Error 101 or a hard permission code on a trades (tick-by-tick) stream frees its line once, closes the handle, sets `error` and calls `on_error`. On a shared depth (reqMktDepth) stream the same failure fails every subscriber on that stream and releases its line once; a later `close()` is a no-op (#129, #136).
- Interactive Brokers: `IbVersionIncompatibleError`. A client/server version refusal (reported by ib_async only via `client.apiError`, followed by a timeout) now fails the connect fast without retrying, and stops the reconnect supervisor with a `disconnected` state. README documents a pinned Gateway version policy (#142, #59).
- `RestConnectorBase.request(..., pools=)`: hb-rate-limit's per-call pool override (0 skips a linked pool; other registered pools are charged ad hoc) (#130, #131).
- `FundingInfo`, `FundingInfoUpdate`, `CancellationResult` and `Position` primitives (frozen Pydantic, canonical `PositionSide`), with drop-in legacy hummingbot classes in `market_connector.hb_compat.{funding_info,cancellation_result,position}` for re-export shims (#27).

### Changed

- **Breaking (hb_compat):** `hb_compat.funding_info.FundingInfo`/`FundingInfoUpdate` now re-export `hb-data-type-primitives` (bumped to v0.4.0); `to_primitive`/`from_primitive` methods replaced by `funding_info_to_primitive`/`funding_info_from_primitive`. Added `hb_compat.primitives.to_canonical`/`from_canonical` name-based enum mapping (#192).
- Kraken: cancels of orders aged 300s or more reserve the declared per-pair endpoint limit with a zero matching-pool charge, instead of going unkeyed through a lazily created default limit (#131).
- Kraken: `cancel_orders_batch` charges every pair's matching pool through one ad-hoc multi-pool reserve; no limit is registered per pair combination (#130).
- Interactive Brokers: connect and reconnect retry warnings log the exception with `repr`, so an empty `TimeoutError()` is no longer a blank message (#142).
- Requires hb-rate-limit v0.5.0.
- hb-logger, hb-data-type-primitives and hb-event-bus are pinned to their signed `v0.1.0` tags instead of commit SHAs, per ADR 0001 (#135).
- The `hummingbot` extra now pins `hb-strategy-framework` to the v0.1.1 release tag instead of an unversioned `>=0.1.0` range (v0.1.1 itself pins hb-data-type-primitives, hb-event-bus and hb-logger @v0.1.0, matching ours).

### Removed

- **BREAKING:** `rate_limits.limiter.ensure_multi_keyed_limit` (added in 0.3.0). Use `RestConnectorBase.request(..., pools=)` instead (#130).

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
