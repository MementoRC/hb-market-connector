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

## Stage roadmap

| Stage | Scope | Status |
|---|---|---|
| 1 | Framework promotion + connector skeleton | ✅ LANDED (#25) |
| 2 | Transport + orders + contract resolver | ✅ LANDED (#26) |
| 3 | Market data + subscriptions mixins | ✅ LANDED (merged) |
| 4 | Accounts mixin (get_balance + subscribe_orders) | Planned |
| 5 | Non-STOCK contracts + subscription multiplexing | Planned |
| 6 | Reconnection / hardening / production polish | Planned |

## Running tests

```bash
# Unit tests (default, no IB Gateway needed)
pixi run test market_connector/exchanges/interactive_brokers/

# Integration tests (requires running IB Gateway)
pixi run test -m ib_gateway
```
