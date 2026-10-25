# Interactive Brokers Integration

**Status:** Stages 1–3 LANDED (framework, transport, market data); Stages 4–6 planned

## Architecture

This integration follows the TransportAwareGateway sub-protocol pattern.
It uses the `unified_transport` slot (one TCP socket carries both
request/response and streaming traffic) and `PassThroughSigner` for
process-managed authentication (IB Gateway holds the session).

See: `~/.claude/plans/2026-05-02-ib-framework-promotion-design.md` for the
full design rationale.

## Running with IB Gateway via Docker

The recommended deployment uses the `gnzsnz/ib-gateway` Docker image.
A minimal `docker-compose.yml`:

```yaml
services:
  ib-gateway:
    image: gnzsnz/ib-gateway:stable
    environment:
      TWS_USERID: ${IBKR_PAPER_USER}
      TWS_PASSWORD: ${IBKR_PAPER_PASSWORD}
      TRADING_MODE: paper
      VNC_SERVER_PASSWORD: ${VNC_PASSWORD}
    ports:
      - "127.0.0.1:4002:4002"
      - "127.0.0.1:5900:5900"
    restart: unless-stopped
```

## Account requirements

- IBKR Pro account (IBKR Lite has NO API access).
- Paper account: register a separate paper login from the IBKR client portal.
- 2FA: use the IBKR Mobile soft-token (TOTP) for headless automation.

## Client IDs and open-order visibility

- Use one stable, unique `client_id` per bot. Order ids come from ib_async's
  `nextValidId` per client, so two bots sharing a `client_id` will collide on
  order ids.
- `client_id=0` is the master client: it also binds orders placed manually in
  TWS (requires TWS "bind automatic orders" / `reqAutoOpenOrders`). Only one
  client can be 0 at a time.
- `fetch_all_open_orders` (default on) calls `reqAllOpenOrdersAsync` on every
  connect and reconnect, so orders from other clients (and, for client 0, TWS)
  appear in `open_orders()` read-only with `owned=False`. IB does not keep
  other clients' orders live-updated after that snapshot.
- `cancel_order` refuses handles with `owned=False` (`ValueError`), and
  `cancel_all` cancels only orders owned by this client; it never issues a
  global cancel.
- Orders placed by a previous run under a different `client_id` are visible
  (`owned=False`) but cannot be cancelled by this bot: IB only lets the
  originating client cancel an order, so keep one stable `client_id` per bot.
- For a `client_id=0` bot, `cancel_all` will skip manual TWS orders (clientId 0
  with orderId <= 0); cancel them explicitly with `cancel_order` if intended.
- `cancel_all` attempts every owned order and raises `CancelAllError` (an
  `ExceptionGroup` carrying `.cancelled`) if any cancel fails.
- `what_if(ref, order)` previews an order's margin, equity-with-loan and
  commission impact (`IbWhatIfResult`, exact `Decimal`, `None` when IB leaves a
  value unset) without placing anything. It raises `WhatIfTimeoutError` after
  `IbConnectionSpec.what_if_timeout` seconds (default 10).
- `get_pnl()` and `get_position_pnl(ref)` return one-shot PnL snapshots
  (`IbPnL` / `IbPositionPnL`, exact `Decimal`; `None` when IB leaves a value
  unset, zero is a real value) for the resolved account. Each call subscribes
  (`reqPnL` / `reqPnLSingle`), takes the first answer and cancels; concurrent
  calls for the same account/position share one request. They raise
  `PnlTimeoutError` after `IbConnectionSpec.pnl_timeout` seconds (default 10).
  `subscribe_pnl(callback)` and `subscribe_position_pnl(ref, callback)` stream
  every update to the callback and return a `SubscriptionHandle`. There is one
  IB request per account/position, shared by all subscribers and snapshots; it
  is cancelled when the last one leaves. While a stream is active, snapshots
  return its latest value (or wait for the first) instead of requesting again.
  Streams survive reconnects and error 1101 (re-issued) and are released by
  `disconnect()`; closing a handle afterwards is a no-op.
  The first IB answer can be stale (seen: daily_pnl 0.0, then -2.0872 a second
  later); for a settled daily figure take a second reading. `get_position_pnl` for a
  contract with no position still answers (position 0, `unrealized_pnl`/`value`
  `None`); it does not time out.
- `place_bracket(ref, entry, take_profit, stop_loss)` and `place_oca(ref, orders,
  oca_type=OcaType.CANCEL_WITH_BLOCK)` place linked orders (`IbBracket` /
  `IbOcaGroup` of handles). If any leg is rejected or does not acknowledge, every
  other leg not known to be rejected (including an unacknowledged one) is cancelled.
  A rejection then raises `LinkedOrderRejectedError` (`.handles`, `.cancel_errors` for
  any failed rollback cancel); any other failure (e.g. the status timeout) re-raises
  the original error.
  Live paper finding: manually cancelling ONE member of an OCA group makes IB cancel
  ALL members (code 202 per leg), so the other handles also end `CANCELLED` and a
  later `cancel_all()` finds nothing.

## Stage roadmap

| Stage | Scope | Status |
|---|---|---|
| 1 | Framework promotion + connector skeleton | ✅ LANDED (#25) |
| 2 | Transport + orders + contract resolver | ✅ LANDED (#26) |
| 3 | Market data + subscriptions mixins | ✅ LANDED (merged) |
| 4 | Accounts (balances, positions, what-if, PnL snapshots, bracket/OCA) | ✅ LANDED |
| 5 | Non-STOCK contracts + subscription multiplexing | Planned |
| 6 | Reconnection / hardening / production polish | Planned |

## Running tests

```bash
# Unit tests (default, no IB Gateway needed)
pixi run test market_connector/exchanges/interactive_brokers/

# Integration tests (requires running IB Gateway)
pixi run test -m ib_gateway
```

Live tests are gated on `IB_GATEWAY_HOST` and skip when it is unset:

- `IB_GATEWAY_HOST` — Gateway host (default `127.0.0.1` once the gate is enabled).
- `IB_GATEWAY_PORT` — Gateway API port (default `4002`, the paper port).

```bash
IB_GATEWAY_HOST=127.0.0.1 IB_GATEWAY_PORT=4012 pixi run test -m ib_gateway
```

`tests/exchanges/interactive_brokers/test_integration_asset_classes_57.py` is a read-only live check of contract resolution for one stock, crypto, forex and futures contract (default `IB_CLIENT_ID=957`).

Live-port guard: the tests fail before connecting if `IB_GATEWAY_PORT` is a known
live-trading default (4001 IB Gateway live, 7496 TWS live). The guard cannot detect
a live Gateway on a non-default port, so always point `IB_GATEWAY_PORT` at a paper
Gateway.

If 4002 is occupied locally, map the Gateway to another host port and set
`IB_GATEWAY_PORT` accordingly. For gnzsnz/ib-gateway the paper API is relayed on
container port 4004, so e.g. `ports: ["127.0.0.1:4012:4004"]` with
`IB_GATEWAY_PORT=4012`.
