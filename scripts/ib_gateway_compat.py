"""Record IB Gateway compatibility facts in the Interactive Brokers README matrix (#144).

Connects read-only to a PAPER Gateway, reads the negotiated server version, runs the
read-only live suite (no orders) and upserts one row into the README matrix.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

MATRIX_START = "<!-- ib-compat-matrix:start -->"
MATRIX_END = "<!-- ib-compat-matrix:end -->"

CHANNELS = ("stable", "latest")
LIVE_TRADING_PORTS = frozenset({4001, 7496})
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_README = REPO_ROOT / "market_connector/exchanges/interactive_brokers/README.md"
SUITE_CMD = [
    sys.executable,
    "-m",
    "pytest",
    "tests/exchanges/interactive_brokers",
    "-m",
    "ib_gateway and not ib_orders",
    "--no-cov",
    "-q",
]
_SUMMARY = re.compile(r"\d+ (?:passed|failed|skipped|errors?)\b")


@dataclass(frozen=True)
class MatrixRow:
    date: str
    channel: str
    gateway_build: str
    server_version: int
    max_client_version: int
    at_client_ceiling: bool
    suite: str
    git_sha: str


def validate_channel(channel: str) -> str:
    if channel not in CHANNELS:
        raise ValueError(f"channel must be one of {CHANNELS}, got {channel!r}")
    return channel


def format_row(row: MatrixRow) -> str:
    ceiling = "yes" if row.at_client_ceiling else "no"
    cells = (
        row.date,
        row.channel,
        row.gateway_build,
        str(row.server_version),
        str(row.max_client_version),
        ceiling,
        row.suite,
        row.git_sha,
    )
    return "| " + " | ".join(cells) + " |"


def upsert_row(text: str, row: MatrixRow) -> str:
    """Replace the row with the same channel and build, else insert it first (after header)."""
    start = text.find(MATRIX_START)
    end = text.find(MATRIX_END)
    if start < 0 or end < 0:
        raise ValueError(f"README must contain {MATRIX_START} and {MATRIX_END}")
    if end < start:
        raise ValueError("matrix end marker precedes start marker")
    body_start = start + len(MATRIX_START)
    lines = text[body_start:end].strip("\n").splitlines()
    key = (row.channel, row.gateway_build)
    new_line = format_row(row)
    for i, line in enumerate(lines[2:], start=2):
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) > 2 and (cells[1], cells[2]) == key:
            lines[i] = new_line
            break
    else:
        lines.insert(2, new_line)
    return f"{text[:body_start]}\n" + "\n".join(lines) + f"\n{text[end:]}"


def _endpoint() -> tuple[str, int]:
    # The live suite skips unless IB_GATEWAY_HOST is set, so require it here too:
    # a defaulted host would record an all-skipped run as a matrix row.
    host = os.environ.get("IB_GATEWAY_HOST")
    if not host:
        raise SystemExit("IB_GATEWAY_HOST must be set (and IB_GATEWAY_PORT for a non-4002 Gateway)")
    raw = os.environ.get("IB_GATEWAY_PORT", "4002")
    try:
        port = int(raw)
    except ValueError:
        raise SystemExit(f"IB_GATEWAY_PORT must be an integer, got {raw!r}") from None
    if port in LIVE_TRADING_PORTS:
        raise SystemExit(
            f"IB_GATEWAY_PORT={port} is a LIVE-trading default port (4001, 7496); "
            "point it at a paper Gateway."
        )
    return host, port


async def _server_info(host: str, port: int) -> tuple[int, int, bool]:
    # run as a file: scripts/ is on sys.path, the repo root is not
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from market_connector.exchanges.interactive_brokers.factory import build_ib_gateway
    from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec

    spec = IbConnectionSpec(
        host=host,
        port=port,
        client_id=int(os.environ.get("IB_CLIENT_ID", "958")),
        account_id=os.environ.get("IB_PAPER_ACCOUNT"),
        paper=True,
    )
    gateway = build_ib_gateway(spec)
    try:
        await gateway.start()
        info = gateway.server_info
        if info is None or info.max_client_version is None:
            raise SystemExit("Gateway did not report server_info")
        return info.server_version, info.max_client_version, info.at_client_ceiling
    finally:
        await gateway.stop()


def _run_suite() -> tuple[str, int]:
    proc = subprocess.run(SUITE_CMD, cwd=REPO_ROOT, capture_output=True, text=True, check=False)
    summary = ""
    for line in reversed(proc.stdout.splitlines()):
        found = _SUMMARY.findall(line)
        if found:
            summary = ", ".join(found)
            break
    return summary or f"exit {proc.returncode}", proc.returncode


def _git_sha() -> str:
    out = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", required=True, choices=CHANNELS)
    parser.add_argument("--gateway-build", default=os.environ.get("IB_GATEWAY_BUILD", "unknown"))
    parser.add_argument("--readme", type=Path, default=DEFAULT_README)
    parser.add_argument("--dry-run", action="store_true", help="print the row only")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse(argv)
    host, port = _endpoint()
    server, ceiling, at_ceiling = asyncio.run(_server_info(host, port))
    suite, code = _run_suite()
    if code == 0 and "passed" not in suite:
        code = 1  # nothing actually ran against the Gateway
    row = MatrixRow(
        date=datetime.now(UTC).date().isoformat(),
        channel=validate_channel(args.channel),
        gateway_build=args.gateway_build,
        server_version=server,
        max_client_version=ceiling,
        at_client_ceiling=at_ceiling,
        suite=suite,
        git_sha=_git_sha(),
    )
    if not args.dry_run:
        text = args.readme.read_text(encoding="utf-8")
        args.readme.write_text(upsert_row(text, row), encoding="utf-8")
    print(format_row(row))
    return 1 if code else 0


if __name__ == "__main__":
    sys.exit(main())
