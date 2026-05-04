# Interactive Brokers Integration

**Status:** Stage 1 (skeleton + connect)

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
| 1 | Skeleton + connect | this PR |
| 2 | Contract resolution + reqContractDetails | TODO |
| 3 | Market data + subscriptions | TODO |
| 4 | Orders gateway-level (LIMIT/MARKET) | TODO |
| 5 | Bridge + conditional orders (STOP/TRAIL) | TODO |
| 6 | Operational hardening (daily reset, cache TTL) | TODO |

## Running tests

```bash
# Unit tests (default, no IB Gateway needed)
pixi run test market_connector/exchanges/interactive_brokers/

# Integration tests (requires running IB Gateway)
pixi run test -m ib_gateway
```
