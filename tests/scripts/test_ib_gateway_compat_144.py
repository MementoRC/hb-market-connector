"""Red acceptance tests for the IB Gateway compat task + matrix (issue #144)."""

from __future__ import annotations

import dataclasses
import importlib.util
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "ib_gateway_compat.py"
PYPROJECT = REPO_ROOT / "pyproject.toml"

README_TEMPLATE = (
    "# Title\n\nintro text\n\n"
    "<!-- ib-compat-matrix:start -->\n"
    "| Date | Channel | Gateway build | Server | Client max | At ceiling | Suite | SHA |\n"
    "|---|---|---|---|---|---|---|---|\n"
    "| 2026-09-01 | stable | unknown | 176 | 178 | no | 6 passed | aaaaaaa |\n"
    "| 2026-08-01 | latest | 10.40 | 177 | 178 | no | 6 passed | bbbbbbb |\n"
    "<!-- ib-compat-matrix:end -->\n\n"
    "trailing text\n"
)


def _load() -> Any:
    assert SCRIPT.is_file(), f"missing {SCRIPT}"
    spec = importlib.util.spec_from_file_location("ib_gateway_compat", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses needs the module registered
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


@pytest.fixture
def mod() -> Any:
    return _load()


def _row(mod: Any, **overrides: Any) -> Any:
    values: dict[str, Any] = {
        "date": "2026-10-27",
        "channel": "stable",
        "gateway_build": "unknown",
        "server_version": 178,
        "max_client_version": 178,
        "at_client_ceiling": True,
        "suite": "6 passed",
        "git_sha": "3b8c752",
    }
    values.update(overrides)
    return mod.MatrixRow(**values)


def _split(text: str, mod: Any) -> tuple[str, list[str], str]:
    head, rest = text.split(mod.MATRIX_START, 1)
    body, tail = rest.split(mod.MATRIX_END, 1)
    return head, body.strip("\n").splitlines(), tail


def test_marker_constants(mod: Any) -> None:
    assert mod.MATRIX_START == "<!-- ib-compat-matrix:start -->"
    assert mod.MATRIX_END == "<!-- ib-compat-matrix:end -->"


def test_matrix_row_is_frozen(mod: Any) -> None:
    row = _row(mod)
    assert dataclasses.is_dataclass(row)
    with pytest.raises(dataclasses.FrozenInstanceError):
        row.channel = "latest"


def test_format_row_ceiling_yes(mod: Any) -> None:
    assert (
        mod.format_row(_row(mod))
        == "| 2026-10-27 | stable | unknown | 178 | 178 | yes | 6 passed | 3b8c752 |"
    )


def test_format_row_ceiling_no(mod: Any) -> None:
    line = mod.format_row(_row(mod, server_version=176, at_client_ceiling=False))
    assert line == "| 2026-10-27 | stable | unknown | 176 | 178 | no | 6 passed | 3b8c752 |"


def test_upsert_inserts_new_row_first(mod: Any) -> None:
    row = _row(mod, channel="latest", gateway_build="10.45")
    out = mod.upsert_row(README_TEMPLATE, row)
    _, lines, _ = _split(out, mod)
    assert len(lines) == 5
    assert lines[:2] == _split(README_TEMPLATE, mod)[1][:2]
    assert lines[2] == mod.format_row(row)
    assert lines[3:] == _split(README_TEMPLATE, mod)[1][2:]


def test_upsert_replaces_same_channel_and_build_in_place(mod: Any) -> None:
    row = _row(mod, channel="latest", gateway_build="10.40", server_version=178)
    out = mod.upsert_row(README_TEMPLATE, row)
    _, lines, _ = _split(out, mod)
    original = _split(README_TEMPLATE, mod)[1]
    assert len(lines) == len(original)
    assert lines[:3] == original[:3]
    assert lines[3] == mod.format_row(row)


def test_upsert_keeps_outside_text_byte_identical(mod: Any) -> None:
    out = mod.upsert_row(README_TEMPLATE, _row(mod, gateway_build="9.99"))
    head, _, tail = _split(out, mod)
    orig_head, _, orig_tail = _split(README_TEMPLATE, mod)
    assert head == orig_head
    assert tail == orig_tail


def test_upsert_is_idempotent(mod: Any) -> None:
    row = _row(mod, gateway_build="9.99")
    once = mod.upsert_row(README_TEMPLATE, row)
    assert mod.upsert_row(once, row) == once


@pytest.mark.parametrize("drop", ["start", "end"])
def test_upsert_missing_marker_raises(mod: Any, drop: str) -> None:
    marker = mod.MATRIX_START if drop == "start" else mod.MATRIX_END
    with pytest.raises(ValueError, match=r"."):
        mod.upsert_row(README_TEMPLATE.replace(marker, ""), _row(mod))


def test_upsert_end_before_start_raises(mod: Any) -> None:
    text = f"{mod.MATRIX_END}\n| x |\n{mod.MATRIX_START}\n"
    with pytest.raises(ValueError, match=r"."):
        mod.upsert_row(text, _row(mod))


@pytest.mark.parametrize("channel", ["stable", "latest"])
def test_validate_channel_accepts(mod: Any, channel: str) -> None:
    assert mod.validate_channel(channel) == channel


@pytest.mark.parametrize("channel", ["beta", "", "Stable"])
def test_validate_channel_rejects(mod: Any, channel: str) -> None:
    with pytest.raises(ValueError, match=r"."):
        mod.validate_channel(channel)


def _pyproject() -> dict[str, Any]:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _all_tasks(data: dict[str, Any]) -> dict[str, str]:
    tasks: dict[str, Any] = dict(data.get("tool", {}).get("pixi", {}).get("tasks", {}))
    for feature in data.get("tool", {}).get("pixi", {}).get("feature", {}).values():
        tasks.update(feature.get("tasks", {}))
    return {k: (v if isinstance(v, str) else str(v.get("cmd", ""))) for k, v in tasks.items()}


def test_ib_orders_marker_registered() -> None:
    markers = _pyproject()["tool"]["pytest"]["ini_options"]["markers"]
    assert any(m.split(":", 1)[0].strip() == "ib_orders" for m in markers)


def test_pixi_task_test_ib_gateway() -> None:
    cmd = _all_tasks(_pyproject()).get("test-ib-gateway")
    assert cmd is not None, "pixi task test-ib-gateway missing"
    assert '-m "ib_gateway and not ib_orders"' in cmd
    assert "tests/exchanges/interactive_brokers" in cmd


def test_pixi_task_ib_gateway_compat() -> None:
    cmd = _all_tasks(_pyproject()).get("ib-gateway-compat")
    assert cmd is not None, "pixi task ib-gateway-compat missing"
    assert "scripts/ib_gateway_compat.py" in cmd


def test_endpoint_requires_host(mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IB_GATEWAY_HOST", raising=False)
    with pytest.raises(SystemExit):
        mod._endpoint()


def test_endpoint_rejects_live_port(mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IB_GATEWAY_HOST", "127.0.0.1")
    monkeypatch.setenv("IB_GATEWAY_PORT", "4001")
    with pytest.raises(SystemExit):
        mod._endpoint()
