# hb-market-connector

Live market connector adapter for Hummingbot — implements `MarketAccessProtocol` + `TradingRulesProtocol` from [hb-strategy-framework](https://github.com/MementoRC/hb-strategy-framework).

## Overview

This package provides `LiveMarketAccess`, a concrete implementation that wraps Hummingbot's `ConnectorBase` to satisfy the strategy framework's market access and trading rules protocols.

## Installation

```bash
pip install hb-market-connector
```

## Development

```bash
pixi install
pixi run check
```

## License

Apache-2.0
