"""Unit tests for the read-only Coinbase user-channel probe script (#310)."""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "coinbase_user_probe_310.py"
SECRET_JWT = "eyJ.secret.jwt"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("coinbase_user_probe_310", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


@pytest.fixture
def mod() -> Any:
    return _load()


class FakeSocket:
    def __init__(self, inbound: list[str]) -> None:
        self.sent: list[str] = []
        self._inbound = list(inbound)
        self.closed = False

    async def send(self, message: str, /) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        if not self._inbound:
            await asyncio.sleep(10)
        return self._inbound.pop(0)

    async def close(self) -> None:
        self.closed = True


class FakeSigner:
    async def transform_outgoing(self, msg: dict[str, Any]) -> dict[str, Any]:
        return {**msg, "jwt": SECRET_JWT}


def _frame(channel: str, seq: int, events: list[dict[str, Any]] | None = None) -> str:
    return json.dumps(
        {"channel": channel, "sequence_num": seq, "events": events or [], "timestamp": "x"}
    )


def _order(cid: str, status: str = "OPEN") -> dict[str, Any]:
    return {"order_id": "o1", "client_order_id": cid, "status": status}


def _records(mod: Any, conn_frames: dict[int, list[tuple[float, str]]]) -> list[dict[str, Any]]:
    rec = mod.Recorder(None)
    for conn, frames in conn_frames.items():
        for mono, raw in frames:
            rec.add({"conn": conn, "dir": "in", "wall": 0.0, "mono": mono, "raw": raw})
    return list(rec.records)


def test_redact_frame_hides_jwt(mod: Any) -> None:
    out = mod.redact_frame({"type": "subscribe", "channel": "user", "jwt": SECRET_JWT})
    assert out["jwt"] == "REDACTED"
    assert out["channel"] == "user"


async def test_subscribe_records_redacted_and_sends_signed(mod: Any) -> None:
    ws = FakeSocket([])
    rec = mod.Recorder(io.StringIO())
    await mod.subscribe(ws, FakeSigner(), rec, 0, None)
    sent = [json.loads(m) for m in ws.sent]
    assert [m["channel"] for m in sent] == ["heartbeats", "user"]
    assert all(m["jwt"] == SECRET_JWT for m in sent)
    assert all("product_ids" not in m for m in sent)
    assert SECRET_JWT not in json.dumps(rec.records)


async def test_product_ids_only_on_user_when_given(mod: Any) -> None:
    ws = FakeSocket([])
    await mod.subscribe(ws, FakeSigner(), mod.Recorder(None), 0, ["BTC-USD"])
    sent = [json.loads(m) for m in ws.sent]
    assert "product_ids" not in sent[0]
    assert sent[1]["product_ids"] == ["BTC-USD"]


async def test_pump_records_inbound_verbatim(mod: Any) -> None:
    raw = _frame("heartbeats", 1)
    sink = io.StringIO()
    rec = mod.Recorder(sink)
    await mod.pump(FakeSocket([raw]), rec, 3, 0.3)
    lines = [json.loads(line) for line in sink.getvalue().splitlines()]
    assert lines[0]["raw"] == raw
    assert lines[0]["conn"] == 3
    assert {"wall", "mono"} <= lines[0].keys()


def test_summary_finds_gaps_and_shared_counter(mod: Any) -> None:
    snap = {"type": "snapshot", "orders": [_order("a") for _ in range(50)]}
    upd = {"type": "update", "orders": [_order(""), {"order_id": "o2", "status": "FILLED"}]}
    frames = [
        (0.0, _frame("user", 1, [snap])),
        (1.0, _frame("heartbeats", 2)),
        (2.0, _frame("user", 3, [upd])),
        (7.0, _frame("heartbeats", 5)),
        (8.0, _frame("user", 6)),
    ]
    summary = mod.summarize(_records(mod, {0: frames}))
    conn = summary["connections"][0]
    assert conn["first_sequence_num"] == 1
    assert conn["gaps_combined"] == [(3, 5)]
    assert conn["gaps_user_only"] == [(1, 3), (3, 6)]
    assert conn["snapshot_batches"] == [50]
    assert conn["final_batch_is_50"] is True
    assert conn["max_idle_gap_s"] == 5.0
    assert summary["updates_empty_client_order_id"] == 2
    assert summary["statuses"] == {"OPEN": 51, "FILLED": 1}
    assert conn["heartbeats_share_counter"].startswith("inconclusive")


def test_summary_clean_shared_counter(mod: Any) -> None:
    frames = [(0.0, _frame("user", 4)), (1.0, _frame("heartbeats", 5)), (2.0, _frame("user", 6))]
    conn = mod.summarize(_records(mod, {1: frames}))["connections"][1]
    assert conn["gaps_combined"] == []
    assert conn["heartbeats_share_counter"].startswith("yes")


def test_summary_separate_counters(mod: Any) -> None:
    frames = [(0.0, _frame("user", 1)), (1.0, _frame("heartbeats", 1)), (2.0, _frame("user", 2))]
    conn = mod.summarize(_records(mod, {0: frames}))["connections"][0]
    assert conn["heartbeats_share_counter"].startswith("no")


def test_summary_subscriptions_and_errors(mod: Any) -> None:
    err = json.dumps({"type": "error", "message": "bad jwt"})
    frames = [(0.0, _frame("subscriptions", 0)), (1.0, err)]
    summary = mod.summarize(_records(mod, {0: frames}))
    assert summary["error_frames"] == ["bad jwt"]
    assert len(summary["subscriptions_frames"]) == 1
    assert "=== SUMMARY ===" in mod.format_summary(summary)


def test_segment_seconds(mod: Any) -> None:
    assert mod.segment_seconds(300, 0) == [300]
    assert mod.segment_seconds(300, 1) == [150, 150]
    assert mod.segment_seconds(300, 3) == [150, 50, 50, 50]


def test_watch_lines_report_latency(mod: Any) -> None:
    frame = {
        "timestamp": "2026-11-27T10:00:00.250000000Z",
        "events": [
            {
                "type": "update",
                "orders": [
                    {"order_id": "o1", "cumulative_quantity": "0.5", "status": "OPEN"},
                    {"order_id": "o9", "cumulative_quantity": "9", "status": "OPEN"},
                ],
            }
        ],
    }
    sent = mod.parse_rfc3339(frame["timestamp"])
    lines = mod.watch_lines(frame, sent + 0.5, "o1", None)
    assert len(lines) == 1
    assert "cumulative_quantity=0.5" in lines[0]
    assert "latency=0.500s" in lines[0]
    assert mod.watch_lines(frame, sent, None, None) == []


def test_script_imports_no_trading_or_rest_code() -> None:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            imported += [base] + [f"{base}.{a.name}" for a in node.names]
    first_party = {i for i in imported if i.startswith("market_connector")}
    assert first_party == {
        "market_connector.exchanges.coinbase.config",
        "market_connector.exchanges.coinbase.config.CoinbaseConfig",
        "market_connector.exchanges.coinbase.ws_auth",
        "market_connector.exchanges.coinbase.ws_auth.CoinbaseJwtWsAuth",
    }
    banned = ("rest", "trading", "place_order", "cancel", "create_order", "mixins", "aiohttp")
    assert not [i for i in imported if any(b in i.lower() for b in banned)]
