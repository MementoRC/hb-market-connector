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
