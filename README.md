# hb-market-connector

Live market connector adapter for Hummingbot — implements `MarketAccessProtocol` + `TradingRulesProtocol` from [hb-strategy-framework](https://github.com/MementoRC/hb-strategy-framework).

## Overview

This package provides `LiveMarketAccess`, a concrete implementation that wraps Hummingbot's `ConnectorBase` to satisfy the strategy framework's market access and trading rules protocols.

## Installation

```bash
pip install hb-market-connector
```

## Interactive Brokers

### Gateway version policy

Run a pinned IB Gateway version (a pinned container tag or installer version) rather than whatever is latest. Upgrade only after running the gated live suites (the #59 matrix) against the new version.

Each connect logs `server version N (ib_async max client version 178)`, so the negotiated version is visible in the logs. If Gateway refuses the client/server version pairing, the connect now fails fast with `IbVersionIncompatibleError` instead of retrying until a timeout (#142), and the reconnect supervisor stops with a `disconnected` state.

## Development

```bash
pixi install
pixi run check
```

## License

Apache-2.0
