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
from websockets.exceptions import ConnectionClosed
from websockets.frames import Close

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
    return [
        {"conn": conn, "dir": "in", "wall": 0.0, "mono": mono, "raw": raw}
        for conn, frames in conn_frames.items()
        for mono, raw in frames
    ]


def test_redact_frame_hides_jwt(mod: Any) -> None:
    out = mod.redact_frame({"type": "subscribe", "channel": "user", "jwt": SECRET_JWT})
    assert out["jwt"] == "REDACTED"
    assert out["channel"] == "user"


async def test_subscribe_records_redacted_and_sends_signed(mod: Any) -> None:
    ws = FakeSocket([])
    sink = io.StringIO()
    rec = mod.Recorder(sink)
    await mod.subscribe(ws, FakeSigner(), rec, 0, None)
    assert SECRET_JWT not in sink.getvalue()
    sent = [json.loads(m) for m in ws.sent]
    assert [m["channel"] for m in sent] == ["heartbeats", "user"]
    assert all(m["jwt"] == SECRET_JWT for m in sent)
    assert all("product_ids" not in m for m in sent)
    assert SECRET_JWT not in json.dumps(rec.stats.summary())


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
    assert conn["heartbeats_share_counter"].startswith("shared")


def test_summary_separate_counters(mod: Any) -> None:
    frames = [(0.0, _frame("user", 1)), (1.0, _frame("heartbeats", 1)), (2.0, _frame("user", 2))]
    conn = mod.summarize(_records(mod, {0: frames}))["connections"][0]
    assert conn["heartbeats_share_counter"].startswith("separate")


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


class ClosingSocket(FakeSocket):
    """Delivers its frames, then the peer closes the connection."""

    async def recv(self) -> str:
        if not self._inbound:
            raise ConnectionClosed(Close(1001, "going away"), None)
        return self._inbound.pop(0)


class _Connect:
    def __init__(self, ws: FakeSocket) -> None:
        self._ws = ws

    async def __aenter__(self) -> FakeSocket:
        return self._ws

    async def __aexit__(self, *exc: object) -> None:
        return None


def _updates(mod: Any, frames: list[tuple[float, str]]) -> dict[str, Any]:
    return mod.summarize(_records(mod, {0: frames}))["connections"][0]


def test_open_output_is_exclusive_and_private(mod: Any, tmp_path: Path) -> None:
    path = tmp_path / "out.jsonl"
    with mod.open_output(path) as sink:
        sink.write("first\n")
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        mod.open_output(path)
    assert path.read_text() == "first\n"
    path.chmod(0o644)
    with mod.open_output(path, overwrite=True) as sink:
        sink.write("second\n")
    assert path.read_text() == "second\n"
    assert path.stat().st_mode & 0o777 == 0o600


def test_default_output_names_are_unique(mod: Any) -> None:
    assert mod.default_output() != mod.default_output()


async def test_main_existing_output_errors_unless_overwrite(
    mod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("COINBASE_API_KEY_NAME", "k")
    monkeypatch.setenv("COINBASE_API_PRIVATE_KEY", "s")
    monkeypatch.setattr(mod, "CoinbaseJwtWsAuth", lambda *_a: FakeSigner())

    async def noop(*_a: Any) -> None:
        return None

    monkeypatch.setattr(mod, "run_probe", noop)
    path = tmp_path / "existing.jsonl"
    path.write_text("keep me")
    args = mod.parse_args(["--output", str(path)])
    assert await mod.main_async(args) == 2
    assert path.read_text() == "keep me"
    assert "--overwrite" in capsys.readouterr().out
    args = mod.parse_args(["--output", str(path), "--overwrite"])
    assert await mod.main_async(args) == 0
    assert path.read_text() == ""
    assert "recording to" in capsys.readouterr().out


def test_load_credentials_accepts_primary_names(mod: Any) -> None:
    env = {
        "COINBASE_API_KEY_NAME": " organizations/o/apiKeys/k ",
        "COINBASE_API_PRIVATE_KEY": "pem",
    }
    assert mod.load_credentials(env) == ("organizations/o/apiKeys/k", "pem")


def test_load_credentials_falls_back_to_legacy_names(mod: Any) -> None:
    env = {"COINBASE_API_KEY": "k", "COINBASE_API_SECRET": "s"}
    assert mod.load_credentials(env) == ("k", "s")


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_load_credentials_treats_blank_as_missing(mod: Any, blank: str) -> None:
    assert (
        mod.load_credentials({"COINBASE_API_KEY_NAME": "k", "COINBASE_API_PRIVATE_KEY": blank})
        is None
    )
    assert (
        mod.load_credentials({"COINBASE_API_KEY_NAME": blank, "COINBASE_API_PRIVATE_KEY": "s"})
        is None
    )
    assert mod.load_credentials({}) is None


def test_load_credentials_normalizes_escaped_newlines(mod: Any) -> None:
    escaped = "-----BEGIN EC PRIVATE KEY-----\\nABC\\n-----END EC PRIVATE KEY-----\\n"
    env = {"COINBASE_API_KEY_NAME": "k", "COINBASE_API_PRIVATE_KEY": escaped}
    _, pem = mod.load_credentials(env)
    assert pem.splitlines() == [
        "-----BEGIN EC PRIVATE KEY-----",
        "ABC",
        "-----END EC PRIVATE KEY-----",
    ]
    real = "-----BEGIN-----\nA\\nB\n-----END-----"
    env["COINBASE_API_PRIVATE_KEY"] = real
    assert mod.load_credentials(env) == ("k", real)


async def test_main_missing_credentials_names_primary_vars(
    mod: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in (
        "COINBASE_API_KEY_NAME",
        "COINBASE_API_PRIVATE_KEY",
        "COINBASE_API_KEY",
        "COINBASE_API_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    assert await mod.main_async(mod.parse_args([])) == 2
    out = capsys.readouterr().out
    assert "COINBASE_API_KEY_NAME" in out
    assert "COINBASE_API_PRIVATE_KEY" in out


def test_recorder_streams_without_retaining_records(mod: Any) -> None:
    sink = io.StringIO()
    rec = mod.Recorder(sink)
    for i in range(100):
        rec.inbound(0, _frame("heartbeats", i))
    assert not hasattr(rec, "records")
    assert len(sink.getvalue().splitlines()) == 100
    assert rec.stats.summary()["connections"][0]["frames"] == 100


def test_recorder_counts_only_after_successful_write(mod: Any) -> None:
    class Broken(io.StringIO):
        def write(self, _s: str, /) -> int:
            raise OSError("disk full")

    rec = mod.Recorder(Broken())
    with pytest.raises(OSError):
        rec.inbound(0, _frame("heartbeats", 1))
    assert rec.stats.summary()["connections"] == {}


def test_heartbeat_sharing_needs_interleaved_plus_one_sequence(mod: Any) -> None:
    # non-overlapping, gap-free, but only one switch: independent counters could look like this
    one_switch = [
        (0.0, _frame("user", 1)),
        (1.0, _frame("user", 2)),
        (2.0, _frame("heartbeats", 3)),
    ]
    assert _updates(mod, one_switch)["heartbeats_share_counter"].startswith("inconclusive")
    interleaved = [(0.0, _frame(c, i)) for i, c in enumerate(["user", "heartbeats", "user"], 1)]
    assert _updates(mod, interleaved)["heartbeats_share_counter"].startswith("shared")


def test_heartbeat_separate_when_each_type_contiguous(mod: Any) -> None:
    frames = [
        (0.0, _frame("user", 1)),
        (1.0, _frame("heartbeats", 10)),
        (2.0, _frame("user", 2)),
        (3.0, _frame("heartbeats", 11)),
    ]
    assert _updates(mod, frames)["heartbeats_share_counter"].startswith("separate")


def test_heartbeat_inconclusive_when_a_type_has_gaps(mod: Any) -> None:
    frames = [(0.0, _frame("user", 1)), (1.0, _frame("heartbeats", 5)), (2.0, _frame("user", 9))]
    assert _updates(mod, frames)["heartbeats_share_counter"].startswith("inconclusive")


@pytest.mark.parametrize(
    "argv",
    [
        ["--duration", "0"],
        ["--duration", "-5"],
        ["--duration", "nan"],
        ["--duration", "inf"],
        ["--duration", "abc"],
        ["--reconnects", "-1"],
        ["--reconnects", "1.5"],
    ],
)
def test_parse_args_rejects_invalid_values(mod: Any, argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        mod.parse_args(argv)
    assert exc.value.code == 2


def test_parse_args_accepts_zero_reconnects(mod: Any) -> None:
    args = mod.parse_args(["--duration", "10", "--reconnects", "0"])
    assert (args.duration, args.reconnects) == (10.0, 0)


def test_watch_lines_snapshot_has_etype_and_no_latency(mod: Any) -> None:
    order = {"order_id": "o1", "cumulative_quantity": "1", "status": "OPEN"}
    frame = {
        "timestamp": "2026-11-27T10:00:00Z",
        "events": [
            {"type": "snapshot", "orders": [order]},
            {"type": "update", "orders": [order]},
        ],
    }
    snap_line, upd_line = mod.watch_lines(
        frame, mod.parse_rfc3339(frame["timestamp"]) + 1, "o1", None
    )
    assert snap_line.startswith("WATCH snapshot ")
    assert snap_line.endswith("latency=n/a")
    assert upd_line.startswith("WATCH update ")
    assert upd_line.endswith("latency=1.000s")


def test_parse_product_ids_strips_and_drops_empties(mod: Any) -> None:
    assert mod.parse_product_ids("BTC-USD, ETH-USD ,, ") == ["BTC-USD", "ETH-USD"]
    assert mod.parse_product_ids(" , ") is None
    assert mod.parse_product_ids(None) is None


def test_snapshot_status_labels(mod: Any) -> None:
    def snap(n: int) -> dict[str, Any]:
        return {"type": "snapshot", "orders": [_order("a") for _ in range(n)]}

    upd = {"type": "update", "orders": []}
    assert _updates(mod, [(0.0, _frame("user", 1, [snap(50)]))])["snapshot_status"].startswith(
        "incomplete"
    )
    short = [(0.0, _frame("user", 1, [snap(50)])), (1.0, _frame("user", 2, [snap(7)]))]
    assert _updates(mod, short)["snapshot_status"].startswith("complete (short")
    followed = [(0.0, _frame("user", 1, [snap(50)])), (1.0, _frame("user", 2, [upd]))]
    assert _updates(mod, followed)["snapshot_status"].startswith("complete (an update")
    assert _updates(mod, [(0.0, _frame("heartbeats", 1))])["snapshot_status"] == "none"


async def test_pump_reports_early_peer_close(mod: Any) -> None:
    rec = mod.Recorder(None)
    end = await mod.pump(ClosingSocket([_frame("heartbeats", 1)]), rec, 0, 5.0)
    assert end.reason == "closed_by_peer"
    assert end.close_code == 1001
    assert end.close_reason == "going away"
    assert end.elapsed_s < 5.0


async def test_run_probe_reconnects_for_remaining_time_after_early_close(
    mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    sockets = [ClosingSocket([_frame("heartbeats", 1)]), FakeSocket([_frame("heartbeats", 1)])]
    monkeypatch.setattr(mod.websockets, "connect", lambda _url: _Connect(sockets.pop(0)))
    monkeypatch.setattr(mod, "RECONNECT_DELAY_S", 0.0)
    rec = mod.Recorder(None)
    args = mod.parse_args(["--duration", "0.5", "--reconnects", "0"])
    await mod.run_probe(args, rec, FakeSigner(), "wss://example.invalid")
    assert not sockets
    summary = rec.stats.summary()
    assert sorted(summary["connections"]) == [0, 1]
    assert summary["unscheduled_connections"] == 1
    assert [s["reason"] for s in summary["short_segments"]] == ["closed_by_peer"]
    text = mod.format_summary(summary)
    assert "closed_by_peer" in text
    assert "going away" in text


async def test_run_probe_gives_up_after_repeated_early_closes(
    mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mod.websockets, "connect", lambda _url: _Connect(ClosingSocket([])))
    monkeypatch.setattr(mod, "RECONNECT_DELAY_S", 0.0)
    monkeypatch.setattr(mod, "MAX_UNSCHEDULED_RECONNECTS", 3)
    rec = mod.Recorder(None)
    args = mod.parse_args(["--duration", "30", "--reconnects", "0"])
    await mod.run_probe(args, rec, FakeSigner(), "wss://example.invalid")
    assert rec.stats.summary()["unscheduled_connections"] == 3


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
