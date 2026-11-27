"""Read-only live probe of the Coinbase Advanced Trade ``user`` WebSocket channel (#310, PR 4 prep).

Run it yourself with your own Coinbase CDP API credentials:

    export COINBASE_API_KEY='organizations/<org>/apiKeys/<key-id>'
    export COINBASE_API_SECRET="$(cat cdp_private_key.pem)"   # EC P-256 PEM
    pixi run python scripts/coinbase_user_probe_310.py --duration 300 --reconnects 1

Optional flags: ``--product-ids BTC-USD,ETH-USD`` (omit for all products), ``--output FILE``
(default ``probe_output/coinbase_user_310_<UTC timestamp>.jsonl``, gitignored),
``--watch-order-id ID`` / ``--watch-client-order-id ID`` (print cumulative_quantity, status and
receive latency of that order's updates; place a small order by hand in the Coinbase UI).

What it captures: it connects to ``CoinbaseConfig(sandbox=False).ws_user_url``, sends separate
``heartbeats`` and ``user`` subscribes (a fresh ES256 JWT per frame) and records every inbound
frame verbatim as JSONL with local receive times (``wall`` = time.time(), ``mono`` =
time.monotonic()) and a connection index. Outbound frames are recorded with the JWT redacted.
After half of ``--duration`` it closes the socket and reconnects ``--reconnects`` times,
resubscribing with fresh JWTs, so the reconnect snapshot is captured. It then prints a SUMMARY:
per-connection first sequence_num, gaps (heartbeats+user together and user-only), whether
heartbeats share the sequence counter, snapshot batch sizes (and whether the final batch had
exactly 50 orders), order updates with an empty/missing client_order_id, status values seen,
``subscriptions`` / ``error`` frames, and the maximum idle gap between frames.

Safety: WebSocket only and read-only. This script never places, modifies or cancels orders and
imports no trading or REST code. Credentials are read from the environment only; the secret, the
api key and any JWT are never printed, logged or written.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Protocol

import websockets
from websockets.exceptions import ConnectionClosed

from market_connector.exchanges.coinbase.config import CoinbaseConfig
from market_connector.exchanges.coinbase.ws_auth import CoinbaseJwtWsAuth

DEFAULT_OUTPUT_DIR = Path("probe_output")
SEQ_CHANNELS = frozenset({"heartbeats", "user"})
RECV_POLL_SECONDS = 1.0
REDACTED = "REDACTED"


class Socket(Protocol):
    async def send(self, message: str, /) -> None: ...
    async def recv(self) -> str | bytes: ...
    async def close(self) -> None: ...


class Signer(Protocol):
    async def transform_outgoing(self, msg: dict[str, Any]) -> dict[str, Any]: ...


def redact_frame(frame: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of an outbound frame with the ``jwt`` field replaced."""
    if "jwt" not in frame:
        return dict(frame)
    return {**frame, "jwt": REDACTED}


class Recorder:
    """Appends one JSON object per line to ``sink`` and keeps the records for the summary."""

    def __init__(self, sink: IO[str] | None) -> None:
        self._sink = sink
        self.records: list[dict[str, Any]] = []

    def add(self, record: dict[str, Any]) -> None:
        self.records.append(record)
        if self._sink is not None:
            self._sink.write(json.dumps(record) + "\n")
            self._sink.flush()

    def inbound(self, conn: int, raw: str) -> dict[str, Any]:
        record: dict[str, Any] = {
            "conn": conn,
            "dir": "in",
            "wall": time.time(),
            "mono": time.monotonic(),
            "raw": raw,
        }
        self.add(record)
        return record

    def outbound(self, conn: int, frame: dict[str, Any]) -> None:
        self.add(
            {
                "conn": conn,
                "dir": "out",
                "wall": time.time(),
                "mono": time.monotonic(),
                "frame": redact_frame(frame),
            }
        )

    def event(self, conn: int, name: str) -> None:
        self.add(
            {
                "conn": conn,
                "dir": "event",
                "wall": time.time(),
                "mono": time.monotonic(),
                "event": name,
            }
        )


def parse_raw(raw: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_rfc3339(value: str) -> float | None:
    """Parse a Coinbase RFC 3339 timestamp (nanosecond precision allowed) to epoch seconds."""
    text = value.strip().replace("Z", "+00:00")
    if "." in text:
        head, _, rest = text.partition(".")
        digits = ""
        for ch in rest:
            if not ch.isdigit():
                break
            digits += ch
        text = f"{head}.{digits[:6].ljust(6, '0')}{rest[len(digits) :]}"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def iter_orders(frame: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Return (event_type, order) pairs of a ``user`` frame."""
    out: list[tuple[str, dict[str, Any]]] = []
    for event in frame.get("events") or []:
        if not isinstance(event, dict):
            continue
        for order in event.get("orders") or []:
            if isinstance(order, dict):
                out.append((str(event.get("type")), order))
    return out


def gaps(seqs: list[int]) -> list[tuple[int, int]]:
    """(previous, current) pairs where the sequence did not advance by exactly 1."""
    return [(a, b) for a, b in zip(seqs, seqs[1:], strict=False) if b != a + 1]


def _heartbeat_sharing(hb: list[int], user: list[int], combined: list[int]) -> str:
    if not hb or not user:
        return "unknown (need both heartbeat and user frames)"
    if set(hb) & set(user):
        return "no (heartbeats and user frames reuse the same numbers: separate counters)"
    if not gaps(combined):
        return "yes (one counter: combined sequence has no gaps)"
    return "inconclusive (combined sequence has gaps)"


def _summarize_connection(frames: list[tuple[float, dict[str, Any]]]) -> dict[str, Any]:
    seq_all: list[int] = []
    seq_user: list[int] = []
    seq_hb: list[int] = []
    batches: list[int] = []
    for _, frame in frames:
        seq = frame.get("sequence_num")
        channel = frame.get("channel")
        if isinstance(seq, int) and channel in SEQ_CHANNELS:
            seq_all.append(seq)
            (seq_user if channel == "user" else seq_hb).append(seq)
        if channel == "user":
            for event in frame.get("events") or []:
                if isinstance(event, dict) and event.get("type") == "snapshot":
                    batches.append(len(event.get("orders") or []))
    all_seqs = [s for _, f in frames if isinstance(s := f.get("sequence_num"), int)]
    monos = [m for m, _ in frames]
    return {
        "frames": len(frames),
        "first_sequence_num": all_seqs[0] if all_seqs else None,
        "gaps_combined": gaps(seq_all),
        "gaps_user_only": gaps(seq_user),
        "heartbeats_share_counter": _heartbeat_sharing(seq_hb, seq_user, seq_all),
        "snapshot_batches": batches,
        "final_batch_is_50": bool(batches) and batches[-1] == 50,
        "max_idle_gap_s": max((b - a for a, b in zip(monos, monos[1:], strict=False)), default=0.0),
    }


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute the probe SUMMARY from recorded frames (pure; no I/O)."""
    per_conn: dict[int, list[tuple[float, dict[str, Any]]]] = {}
    statuses: Counter[str] = Counter()
    channels: Counter[str] = Counter()
    empty_cid = {"update": 0, "snapshot": 0}
    updates = 0
    subscriptions: list[Any] = []
    errors: list[Any] = []
    for rec in records:
        if rec.get("dir") != "in":
            continue
        frame = parse_raw(rec["raw"])
        if frame is None:
            continue
        per_conn.setdefault(rec["conn"], []).append((rec["mono"], frame))
        channels[str(frame.get("channel"))] += 1
        if frame.get("channel") == "subscriptions":
            subscriptions.append(frame.get("events"))
        if frame.get("type") == "error" or frame.get("channel") == "error":
            errors.append(frame.get("message") or frame)
        for etype, order in iter_orders(frame):
            statuses[str(order.get("status"))] += 1
            if etype == "update":
                updates += 1
            if etype in empty_cid and not order.get("client_order_id"):
                empty_cid[etype] += 1
    connections = {c: _summarize_connection(f) for c, f in sorted(per_conn.items())}
    return {
        "connections": connections,
        "order_updates": updates,
        "updates_empty_client_order_id": empty_cid["update"],
        "snapshot_orders_empty_client_order_id": empty_cid["snapshot"],
        "statuses": dict(statuses),
        "channels": dict(channels),
        "subscriptions_frames": subscriptions,
        "error_frames": errors,
        "max_idle_gap_s": max((c["max_idle_gap_s"] for c in connections.values()), default=0.0),
    }


def format_summary(summary: dict[str, Any]) -> str:
    lines = ["=== SUMMARY ==="]
    for conn, c in summary["connections"].items():
        lines += [
            f"conn {conn}: frames={c['frames']} first_sequence_num={c['first_sequence_num']}",
            f"  gaps (heartbeats+user): {c['gaps_combined'] or 'none'}",
            f"  gaps (user only): {c['gaps_user_only'] or 'none'}",
            f"  heartbeats share counter: {c['heartbeats_share_counter']}",
            f"  snapshot batches: {c['snapshot_batches']} final batch is 50: "
            f"{c['final_batch_is_50']}",
            f"  max idle gap: {c['max_idle_gap_s']:.2f}s",
        ]
    lines += [
        f"order updates: {summary['order_updates']} "
        f"with empty/missing client_order_id: {summary['updates_empty_client_order_id']} "
        f"(snapshot orders: {summary['snapshot_orders_empty_client_order_id']})",
        f"statuses seen: {summary['statuses']}",
        f"channels seen: {summary['channels']}",
        f"subscriptions frames: {summary['subscriptions_frames']}",
        f"error frames: {summary['error_frames']}",
        f"max idle gap overall: {summary['max_idle_gap_s']:.2f}s",
    ]
    return "\n".join(lines)


def watch_lines(
    frame: dict[str, Any], recv_wall: float, order_id: str | None, client_order_id: str | None
) -> list[str]:
    """One line per matching order update: cumulative_quantity, status and receive latency."""
    if not order_id and not client_order_id:
        return []
    sent = parse_rfc3339(str(frame.get("timestamp", "")))
    latency = f"{recv_wall - sent:.3f}s" if sent is not None else "n/a"
    lines = []
    for etype, order in iter_orders(frame):
        if (order_id and order.get("order_id") == order_id) or (
            client_order_id and order.get("client_order_id") == client_order_id
        ):
            lines.append(
                f"WATCH {etype} order_id={order.get('order_id')} "
                f"cumulative_quantity={order.get('cumulative_quantity')} "
                f"status={order.get('status')} latency={latency}"
            )
    return lines


async def subscribe(
    ws: Socket, signer: Signer, rec: Recorder, conn: int, product_ids: list[str] | None
) -> None:
    for channel in ("heartbeats", "user"):
        frame: dict[str, Any] = {"type": "subscribe", "channel": channel}
        if channel == "user" and product_ids:
            frame["product_ids"] = product_ids
        signed = await signer.transform_outgoing(frame)  # fresh JWT per frame
        await ws.send(json.dumps(signed))
        rec.outbound(conn, signed)


async def pump(
    ws: Socket,
    rec: Recorder,
    conn: int,
    seconds: float,
    watch: tuple[str | None, str | None] = (None, None),
) -> None:
    """Record inbound frames until ``seconds`` elapse or the socket closes."""
    deadline = time.monotonic() + seconds
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            msg = await asyncio.wait_for(ws.recv(), min(RECV_POLL_SECONDS, remaining))
        except TimeoutError:
            continue
        except ConnectionClosed:
            rec.event(conn, "closed_by_peer")
            return
        raw = msg if isinstance(msg, str) else msg.decode("utf-8", "replace")
        record = rec.inbound(conn, raw)
        frame = parse_raw(raw)
        if frame is not None:
            for line in watch_lines(frame, record["wall"], *watch):
                print(line, flush=True)


def segment_seconds(duration: float, reconnects: int) -> list[float]:
    """First connection runs half the duration; the reconnects share the other half."""
    if reconnects <= 0:
        return [duration]
    half = duration / 2
    return [half] + [half / reconnects] * reconnects


async def run_probe(args: argparse.Namespace, rec: Recorder, signer: Signer, url: str) -> None:
    product_ids = [p for p in (args.product_ids or "").split(",") if p] or None
    watch = (args.watch_order_id, args.watch_client_order_id)
    for conn, seconds in enumerate(segment_seconds(args.duration, args.reconnects)):
        async with websockets.connect(url) as ws:
            rec.event(conn, "connected")
            await subscribe(ws, signer, rec, conn, product_ids)
            await pump(ws, rec, conn, seconds, watch)
            await ws.close()
        rec.event(conn, "closed_by_probe")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Read-only Coinbase user-channel probe (#310).")
    p.add_argument("--duration", type=float, default=300.0, help="total seconds (default 300)")
    p.add_argument("--reconnects", type=int, default=1, help="reconnects after half (default 1)")
    p.add_argument("--product-ids", default=None, help="comma-separated; omit for all products")
    p.add_argument("--output", type=Path, default=None, help="JSONL path (default probe_output/)")
    p.add_argument("--watch-order-id", default=None, help="print updates of this order_id")
    p.add_argument("--watch-client-order-id", default=None, help="print updates of this client id")
    return p.parse_args(argv)


def default_output() -> Path:
    return DEFAULT_OUTPUT_DIR / f"coinbase_user_310_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.jsonl"


async def main_async(args: argparse.Namespace) -> int:
    api_key = os.environ.get("COINBASE_API_KEY")
    secret = os.environ.get("COINBASE_API_SECRET")
    if not api_key or not secret:
        print("set COINBASE_API_KEY and COINBASE_API_SECRET (EC PEM) in the environment")
        return 2
    config = CoinbaseConfig(api_key=api_key, secret_key=secret, sandbox=False)
    signer = CoinbaseJwtWsAuth(api_key, secret)
    out = args.output or default_output()
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"recording to {out}; connecting to {config.ws_user_url}")
    with out.open("w", encoding="utf-8") as sink:
        rec = Recorder(sink)
        try:
            await run_probe(args, rec, signer, config.ws_user_url)
        finally:
            print(format_summary(summarize(rec.records)))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async(parse_args())))
