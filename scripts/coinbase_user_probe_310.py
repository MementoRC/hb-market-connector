"""Read-only live probe of the Coinbase Advanced Trade ``user`` WebSocket channel (#310, PR 4 prep).

Run it yourself with your own Coinbase CDP API credentials:

    export COINBASE_API_KEY_NAME='organizations/<org>/apiKeys/<key-id>'
    export COINBASE_API_PRIVATE_KEY="$(cat cdp_private_key.pem)"   # EC P-256 PEM

(a PEM with literal backslash-n escapes instead of newlines is also accepted; the older names
COINBASE_API_KEY / COINBASE_API_SECRET work as fallbacks)
    pixi run probe-coinbase-user --duration 300 --reconnects 1

``probe-coinbase-user`` is a pixi task (default env, which has the websockets, pyjwt and
cryptography dependencies) that runs ``python scripts/coinbase_user_probe_310.py``. The package is
not installed into the env: running a file puts ``scripts/`` (not the repo root) on ``sys.path``,
so the script inserts the repo root itself before importing ``market_connector``. Check the wiring
without credentials with ``pixi run probe-coinbase-user --help``.

Optional flags: ``--product-ids BTC-USD,ETH-USD`` (omit for all products), ``--output FILE``
(default ``probe_output/coinbase_user_310_<UTC timestamp>_<unique suffix>.jsonl``, gitignored),
``--overwrite`` (allow replacing an existing ``--output`` file; otherwise it is an error),
``--watch-order-id ID`` / ``--watch-client-order-id ID`` (print cumulative_quantity, status and,
for updates, receive latency of that order's events; place a small order by hand in the Coinbase
UI).

What it captures: it connects to ``CoinbaseConfig(sandbox=False).ws_user_url``, sends separate
``heartbeats`` and ``user`` subscribes (a fresh ES256 JWT per frame) and records every inbound
frame verbatim as JSONL with local receive times (``wall`` = time.time(), ``mono`` =
time.monotonic()) and a connection index. Outbound frames are recorded with the JWT redacted.
The capture file is created exclusively with mode 0600 and records are streamed to disk; the
summary is computed incrementally. After half of ``--duration`` it closes the socket and
reconnects ``--reconnects`` times, resubscribing with fresh JWTs, so the reconnect snapshot is
captured. If the peer closes a segment early, the probe records the close code, reason and timing
and reconnects for the remaining time (an unscheduled connection). It then prints a SUMMARY:
per-connection first sequence_num, gaps (heartbeats+user together and user-only), whether
heartbeats share the sequence counter (shared / separate / inconclusive), snapshot batch sizes
(and whether the snapshot was completed), order updates with an empty/missing client_order_id,
status values seen, ``subscriptions`` / ``error`` frames, short or early-closed segments, and the
maximum idle gap between frames.

Safety: WebSocket only and read-only. This script never places, modifies or cancels orders and
imports no trading or REST code. Credentials are read from the environment only; the secret, the
api key and any JWT are never printed, logged or written.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Protocol

import websockets
from websockets.exceptions import ConnectionClosed

# Run as a file, sys.path[0] is scripts/, not the repo root, and the package is not installed.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from market_connector.exchanges.coinbase.config import CoinbaseConfig  # noqa: E402
from market_connector.exchanges.coinbase.ws_auth import CoinbaseJwtWsAuth  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Mapping

DEFAULT_OUTPUT_DIR = Path("probe_output")
SEQ_CHANNELS = frozenset({"heartbeats", "user"})
RECV_POLL_SECONDS = 1.0
REDACTED = "REDACTED"
SNAPSHOT_BATCH_SIZE = 50
MAX_UNSCHEDULED_RECONNECTS = 10
RECONNECT_DELAY_S = 1.0
MAX_KEPT_FRAMES = 100
MIN_INTERLEAVE_SWITCHES = 2
OUTPUT_MODE = 0o600


class Socket(Protocol):
    async def send(self, message: str, /) -> None: ...
    async def recv(self) -> str | bytes: ...
    async def close(self) -> None: ...


class Signer(Protocol):
    async def transform_outgoing(self, msg: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class SegmentEnd:
    """How one connection segment ended."""

    reason: str  # "scheduled" or "closed_by_peer"
    elapsed_s: float
    close_code: int | None = None
    close_reason: str | None = None


def redact_frame(frame: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of an outbound frame with the ``jwt`` field replaced."""
    if "jwt" not in frame:
        return dict(frame)
    return {**frame, "jwt": REDACTED}


def gaps(seqs: list[int]) -> list[tuple[int, int]]:
    """(previous, current) pairs where the sequence did not advance by exactly 1."""
    return [(a, b) for a, b in zip(seqs, seqs[1:], strict=False) if b != a + 1]


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


def _advance(last: int | None, seq: int, found: list[tuple[int, int]]) -> int:
    if last is not None and seq != last + 1:
        found.append((last, seq))
    return seq


class _ConnStats:
    """Running per-connection statistics (no frames are retained)."""

    def __init__(self) -> None:
        self.frames = 0
        self.first_seq: int | None = None
        self.last_all: int | None = None
        self.last_hb: int | None = None
        self.last_user: int | None = None
        self.last_channel: str | None = None
        self.switches = 0
        self.hb_frames = 0
        self.user_frames = 0
        self.gaps_all: list[tuple[int, int]] = []
        self.gaps_hb: list[tuple[int, int]] = []
        self.gaps_user: list[tuple[int, int]] = []
        self.batches: list[int] = []
        self.update_after_snapshot = False
        self.last_mono: float | None = None
        self.max_idle = 0.0
        self.unscheduled = False

    def add_frame(self, mono: float, frame: dict[str, Any]) -> None:
        self.frames += 1
        if self.last_mono is not None:
            self.max_idle = max(self.max_idle, mono - self.last_mono)
        self.last_mono = mono
        seq = frame.get("sequence_num")
        channel = frame.get("channel")
        if isinstance(seq, int) and self.first_seq is None:
            self.first_seq = seq
        if isinstance(seq, int) and channel in SEQ_CHANNELS:
            self.last_all = _advance(self.last_all, seq, self.gaps_all)
            if channel == "user":
                self.user_frames += 1
                self.last_user = _advance(self.last_user, seq, self.gaps_user)
            else:
                self.hb_frames += 1
                self.last_hb = _advance(self.last_hb, seq, self.gaps_hb)
            if self.last_channel is not None and channel != self.last_channel:
                self.switches += 1
            self.last_channel = channel
        if channel == "user":
            for event in frame.get("events") or []:
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "snapshot":
                    self.batches.append(len(event.get("orders") or []))
                    self.update_after_snapshot = False
                elif event.get("type") == "update" and self.batches:
                    self.update_after_snapshot = True

    def heartbeat_sharing(self) -> str:
        if not self.hb_frames or not self.user_frames:
            return "unknown (need both heartbeat and user frames)"
        if not self.gaps_all and self.switches >= MIN_INTERLEAVE_SWITCHES:
            return "shared (heartbeat and user frames interleave in one +1 sequence)"
        if self.gaps_all and not self.gaps_hb and not self.gaps_user:
            return "separate (each type is contiguous alone, the combined sequence is not)"
        return "inconclusive (no positive evidence either way)"

    def snapshot_status(self) -> str:
        if not self.batches:
            return "none"
        if self.batches[-1] < SNAPSHOT_BATCH_SIZE:
            return "complete (short final batch)"
        if self.update_after_snapshot:
            return "complete (an update followed the last batch)"
        return "incomplete (connection ended before a short batch or an update)"

    def summary(self) -> dict[str, Any]:
        return {
            "frames": self.frames,
            "first_sequence_num": self.first_seq,
            "gaps_combined": self.gaps_all,
            "gaps_user_only": self.gaps_user,
            "heartbeats_share_counter": self.heartbeat_sharing(),
            "snapshot_batches": self.batches,
            "final_batch_is_50": bool(self.batches) and self.batches[-1] == SNAPSHOT_BATCH_SIZE,
            "snapshot_status": self.snapshot_status(),
            "max_idle_gap_s": self.max_idle,
            "unscheduled": self.unscheduled,
        }


class SummaryAccumulator:
    """Computes the probe SUMMARY incrementally from records, retaining none of them."""

    def __init__(self) -> None:
        self._conns: dict[int, _ConnStats] = {}
        self._statuses: Counter[str] = Counter()
        self._channels: Counter[str] = Counter()
        self._empty_cid = {"update": 0, "snapshot": 0}
        self._updates = 0
        self._subscriptions: list[Any] = []
        self._errors: list[Any] = []
        self._segments: list[dict[str, Any]] = []

    def _conn(self, conn: int) -> _ConnStats:
        return self._conns.setdefault(conn, _ConnStats())

    def add(self, rec: dict[str, Any]) -> None:
        if rec.get("dir") == "event":
            self._add_event(rec)
            return
        if rec.get("dir") != "in":
            return
        frame = parse_raw(rec["raw"])
        if frame is None:
            return
        self._conn(rec["conn"]).add_frame(rec["mono"], frame)
        self._channels[str(frame.get("channel"))] += 1
        if frame.get("channel") == "subscriptions" and len(self._subscriptions) < MAX_KEPT_FRAMES:
            self._subscriptions.append(frame.get("events"))
        is_error = frame.get("type") == "error" or frame.get("channel") == "error"
        if is_error and len(self._errors) < MAX_KEPT_FRAMES:
            self._errors.append(frame.get("message") or frame)
        for etype, order in iter_orders(frame):
            self._statuses[str(order.get("status"))] += 1
            if etype == "update":
                self._updates += 1
            if etype in self._empty_cid and not order.get("client_order_id"):
                self._empty_cid[etype] += 1

    def _add_event(self, rec: dict[str, Any]) -> None:
        conn = rec["conn"]
        if rec.get("event") == "connected":
            self._conn(conn).unscheduled = not rec.get("scheduled", True)
        elif rec.get("event") == "segment_end":
            self._segments.append(
                {
                    "conn": conn,
                    "reason": rec.get("reason"),
                    "scheduled_s": rec.get("scheduled_s"),
                    "elapsed_s": rec.get("elapsed_s"),
                    "close_code": rec.get("close_code"),
                    "close_reason": rec.get("close_reason"),
                }
            )

    def summary(self) -> dict[str, Any]:
        connections = {c: s.summary() for c, s in sorted(self._conns.items())}
        short = [s for s in self._segments if s["reason"] != "scheduled"]
        return {
            "connections": connections,
            "order_updates": self._updates,
            "updates_empty_client_order_id": self._empty_cid["update"],
            "snapshot_orders_empty_client_order_id": self._empty_cid["snapshot"],
            "statuses": dict(self._statuses),
            "channels": dict(self._channels),
            "subscriptions_frames": self._subscriptions,
            "error_frames": self._errors,
            "short_segments": short,
            "unscheduled_connections": sum(1 for c in connections.values() if c["unscheduled"]),
            "max_idle_gap_s": max((c["max_idle_gap_s"] for c in connections.values()), default=0.0),
        }


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute the probe SUMMARY from a list of records (pure; no I/O)."""
    acc = SummaryAccumulator()
    for rec in records:
        acc.add(rec)
    return acc.summary()


class Recorder:
    """Appends one JSON object per line to ``sink``; keeps only running summary statistics."""

    def __init__(self, sink: IO[str] | None) -> None:
        self._sink = sink
        self.stats = SummaryAccumulator()

    def add(self, record: dict[str, Any]) -> None:
        if self._sink is not None:
            self._sink.write(json.dumps(record) + "\n")
            self._sink.flush()
        self.stats.add(record)  # only after the record is safely written

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

    def event(self, conn: int, name: str, **extra: Any) -> None:
        self.add(
            {
                "conn": conn,
                "dir": "event",
                "wall": time.time(),
                "mono": time.monotonic(),
                "event": name,
                **extra,
            }
        )


def format_summary(summary: dict[str, Any]) -> str:
    lines = ["=== SUMMARY ==="]
    for conn, c in summary["connections"].items():
        kind = " (unscheduled reconnect)" if c.get("unscheduled") else ""
        lines += [
            f"conn {conn}{kind}: frames={c['frames']} first_sequence_num={c['first_sequence_num']}",
            f"  gaps (heartbeats+user): {c['gaps_combined'] or 'none'}",
            f"  gaps (user only): {c['gaps_user_only'] or 'none'}",
            f"  heartbeats share counter: {c['heartbeats_share_counter']}",
            f"  snapshot batches: {c['snapshot_batches']} final batch is 50: "
            f"{c['final_batch_is_50']}; snapshot: {c['snapshot_status']}",
            f"  max idle gap: {c['max_idle_gap_s']:.2f}s",
        ]
    short = summary.get("short_segments", [])
    lines.append(f"short/early-closed segments: {len(short) or 'none'}")
    for s in short:
        lines.append(
            f"  conn {s['conn']}: {s['reason']} after {s['elapsed_s']:.1f}s of "
            f"{s['scheduled_s']:.1f}s scheduled "
            f"(code={s['close_code']} reason={s['close_reason']!r})"
        )
    lines += [
        f"unscheduled reconnects: {summary.get('unscheduled_connections', 0)}",
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
    """One line per matching order event; receive latency is computed for updates only."""
    if not order_id and not client_order_id:
        return []
    sent = parse_rfc3339(str(frame.get("timestamp", "")))
    lines = []
    for etype, order in iter_orders(frame):
        if (order_id and order.get("order_id") == order_id) or (
            client_order_id and order.get("client_order_id") == client_order_id
        ):
            latency = (
                f"{recv_wall - sent:.3f}s" if etype == "update" and sent is not None else "n/a"
            )
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
) -> SegmentEnd:
    """Record inbound frames until ``seconds`` elapse or the socket closes."""
    start = time.monotonic()
    deadline = start + seconds
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            msg = await asyncio.wait_for(ws.recv(), min(RECV_POLL_SECONDS, remaining))
        except TimeoutError:
            continue
        except ConnectionClosed as exc:
            frame_close = getattr(exc, "rcvd", None)
            return SegmentEnd(
                "closed_by_peer",
                time.monotonic() - start,
                getattr(frame_close, "code", None),
                getattr(frame_close, "reason", None) or None,
            )
        raw = msg if isinstance(msg, str) else msg.decode("utf-8", "replace")
        record = rec.inbound(conn, raw)
        frame = parse_raw(raw)
        if frame is not None:
            for line in watch_lines(frame, record["wall"], *watch):
                print(line, flush=True)
    return SegmentEnd("scheduled", time.monotonic() - start)


def segment_seconds(duration: float, reconnects: int) -> list[float]:
    """First connection runs half the duration; the reconnects share the other half."""
    if reconnects <= 0:
        return [duration]
    half = duration / 2
    return [half] + [half / reconnects] * reconnects


def parse_product_ids(text: str | None) -> list[str] | None:
    """Split a comma-separated list, stripping whitespace and dropping empties."""
    ids = [p.strip() for p in (text or "").split(",")]
    return [p for p in ids if p] or None


async def run_probe(args: argparse.Namespace, rec: Recorder, signer: Signer, url: str) -> None:
    product_ids = parse_product_ids(args.product_ids)
    watch = (args.watch_order_id, args.watch_client_order_id)
    conn = -1
    unscheduled = 0
    for scheduled in segment_seconds(args.duration, args.reconnects):
        seg_deadline = time.monotonic() + scheduled
        first = True
        while (remaining := seg_deadline - time.monotonic()) > 0 or first:
            if not first:
                if unscheduled >= MAX_UNSCHEDULED_RECONNECTS:
                    rec.event(conn, "gave_up_unscheduled_reconnects", limit=unscheduled)
                    return
                unscheduled += 1
                await asyncio.sleep(RECONNECT_DELAY_S)
                remaining = seg_deadline - time.monotonic()
                if remaining <= 0:
                    break
            conn += 1
            async with websockets.connect(url) as ws:
                rec.event(conn, "connected", scheduled=first)
                await subscribe(ws, signer, rec, conn, product_ids)
                end = await pump(ws, rec, conn, remaining, watch)
                if end.reason == "scheduled":
                    await ws.close()
            rec.event(
                conn,
                "segment_end",
                reason=end.reason,
                scheduled_s=remaining,
                elapsed_s=end.elapsed_s,
                close_code=end.close_code,
                close_reason=end.close_reason,
            )
            if end.reason == "scheduled":
                break
            first = False


def _positive_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from exc
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be a finite number > 0")
    return value


def _non_negative_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from exc
    if value < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Read-only Coinbase user-channel probe (#310).",
        epilog=(
            "Credentials come from the environment: COINBASE_API_KEY_NAME (CDP key name) and "
            "COINBASE_API_PRIVATE_KEY (EC PEM)."
        ),
    )
    p.add_argument(
        "--duration", type=_positive_float, default=300.0, help="total seconds > 0 (default 300)"
    )
    p.add_argument(
        "--reconnects",
        type=_non_negative_int,
        default=1,
        help="reconnects after half, >= 0 (default 1)",
    )
    p.add_argument("--product-ids", default=None, help="comma-separated; omit for all products")
    p.add_argument("--output", type=Path, default=None, help="JSONL path (default probe_output/)")
    p.add_argument(
        "--overwrite", action="store_true", help="allow replacing an existing --output file"
    )
    p.add_argument("--watch-order-id", default=None, help="print updates of this order_id")
    p.add_argument("--watch-client-order-id", default=None, help="print updates of this client id")
    return p.parse_args(argv)


def default_output() -> Path:
    stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}_{uuid.uuid4().hex[:8]}"
    return DEFAULT_OUTPUT_DIR / f"coinbase_user_310_{stamp}.jsonl"


def open_output(path: Path, overwrite: bool = False) -> IO[str]:
    """Create ``path`` with mode 0600; fail if it exists unless ``overwrite``."""
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if overwrite else os.O_EXCL)
    fd = os.open(path, flags, OUTPUT_MODE)
    if overwrite and hasattr(os, "fchmod"):
        os.fchmod(fd, OUTPUT_MODE)
    return os.fdopen(fd, "w", encoding="utf-8")


def _env_value(env: Mapping[str, str], *names: str) -> str | None:
    """First non-blank value among ``names`` (whitespace-stripped), else None."""
    for name in names:
        value = env.get(name, "").strip()
        if value:
            return value
    return None


def load_credentials(env: Mapping[str, str]) -> tuple[str, str] | None:
    """Return (key name, PEM) from the environment, or None if either is missing or blank.

    Primary names are COINBASE_API_KEY_NAME / COINBASE_API_PRIVATE_KEY; COINBASE_API_KEY /
    COINBASE_API_SECRET are accepted as fallbacks. A PEM with literal ``\\n`` sequences and no
    real newlines is converted to real newlines.
    """
    api_key = _env_value(env, "COINBASE_API_KEY_NAME", "COINBASE_API_KEY")
    secret = _env_value(env, "COINBASE_API_PRIVATE_KEY", "COINBASE_API_SECRET")
    if api_key is None or secret is None:
        return None
    if "\n" not in secret:
        secret = secret.replace("\\n", "\n")
    return api_key, secret


async def main_async(args: argparse.Namespace) -> int:
    creds = load_credentials(os.environ)
    if creds is None:
        print("set COINBASE_API_KEY_NAME and COINBASE_API_PRIVATE_KEY (EC PEM) in the environment")
        return 2
    api_key, secret = creds
    config = CoinbaseConfig(api_key=api_key, secret_key=secret, sandbox=False)
    signer = CoinbaseJwtWsAuth(api_key, secret)
    out = args.output or default_output()
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        sink = open_output(out, overwrite=args.overwrite)
    except FileExistsError:
        print(f"error: {out} already exists; choose another --output or pass --overwrite")
        return 2
    print(f"recording to {out}; connecting to {config.ws_user_url}")
    with sink:
        rec = Recorder(sink)
        try:
            await run_probe(args, rec, signer, config.ws_user_url)
        finally:
            print(format_summary(rec.stats.summary()))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async(parse_args())))
