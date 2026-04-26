"""
Automated fixture capture for hb-coinbase-connector.

Usage:
    python -m market_connector.exchanges.coinbase.tools.fixture_recorder \\
        --api-key $KEY --secret $SECRET \\
        --output tests/fixtures/ \\
        --endpoints products,accounts,product_book,candles,server_time
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path
from typing import Any

from market_connector.exchanges.coinbase.coinbase_gateway import CoinbaseGateway
from market_connector.exchanges.coinbase.config import CoinbaseConfig

_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")


def sanitize(obj: Any) -> Any:
    """Replace sensitive fields (UUIDs, API keys) with deterministic fakes."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in {"api_key", "secret", "email", "user_id"}:
                out[k] = f"REDACTED_{k.upper()}"
            else:
                out[k] = sanitize(v)
        return out
    if isinstance(obj, list):
        return [sanitize(x) for x in obj]
    if isinstance(obj, str) and _UUID_RE.search(obj):
        return _UUID_RE.sub("00000000-0000-0000-0000-000000000001", obj)
    return obj


async def capture_rest(gw: CoinbaseGateway, endpoint: str, output_dir: Path) -> None:
    """Capture a single REST endpoint response and write sanitized JSON to disk."""
    params_map: dict[str, dict[str, Any]] = {
        "server_time": {},
        "accounts": {},
        "products": {},
        "product_book": {"product_id": "BTC-USD"},
        "candles": {"product_id": "BTC-USD", "granularity": "ONE_HOUR"},
        "order_status": {"order_status": "OPEN"},
    }
    try:
        response = await gw._rest.request(endpoint, params=params_map.get(endpoint, {}))
        raw = response.raw
        sanitized = sanitize(raw)
        output_file = output_dir / f"{endpoint}.json"
        output_file.write_text(json.dumps(sanitized, indent=2))
        print(f"Captured {endpoint} -> {output_file}")
    except Exception as e:  # noqa: BLE001
        print(f"Failed to capture {endpoint}: {e}")


async def main() -> None:
    """Parse CLI arguments, create gateway, and capture requested endpoints."""
    parser = argparse.ArgumentParser(
        description="Capture Coinbase API responses as JSON fixtures for offline tests."
    )
    parser.add_argument("--api-key", required=True, help="Coinbase Advanced Trade API key")
    parser.add_argument("--secret", required=True, help="Coinbase Advanced Trade secret key")
    parser.add_argument("--output", type=Path, required=True, help="Output directory for fixtures")
    parser.add_argument(
        "--endpoints",
        default="server_time,accounts,products,product_book,candles",
        help="Comma-separated list of endpoints to capture",
    )
    parser.add_argument("--sandbox", action="store_true", help="Use sandbox API URLs")
    args = parser.parse_args()

    cfg = CoinbaseConfig(api_key=args.api_key, secret_key=args.secret, sandbox=args.sandbox)
    gw = CoinbaseGateway(cfg)
    await gw.start()

    rest_dir: Path = args.output / "rest"
    rest_dir.mkdir(parents=True, exist_ok=True)

    for ep in args.endpoints.split(","):
        await capture_rest(gw, ep.strip(), rest_dir)

    await gw.stop()


if __name__ == "__main__":
    asyncio.run(main())
