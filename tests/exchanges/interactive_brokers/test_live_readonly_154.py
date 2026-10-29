"""Red acceptance tests: live IB suite runs against a READ_ONLY_API Gateway (issue #154)."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from typing import Any

import pytest

IB_TESTS = Path(__file__).resolve().parent


class FakeMarker:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeItem:
    def __init__(self, *marks: str) -> None:
        self._marks = set(marks)
        self.added: list[Any] = []

    def get_closest_marker(self, name: str) -> FakeMarker | None:
        return FakeMarker(name) if name in self._marks else None

    def add_marker(self, marker: Any) -> None:
        self.added.append(marker)


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", "Yes", "On"])
def test_live_readonly_truthy(value: str) -> None:
    from tests.exchanges.interactive_brokers import _live

    assert _live.live_readonly({"IB_READONLY": value}) is True


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"IB_READONLY": ""},
        {"IB_READONLY": "0"},
        {"IB_READONLY": "false"},
        {"IB_READONLY": "No"},
    ],
)
def test_live_readonly_falsy(env: dict[str, str]) -> None:
    from tests.exchanges.interactive_brokers import _live

    assert _live.live_readonly(env) is False


def test_live_readonly_defaults_to_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.exchanges.interactive_brokers import _live

    monkeypatch.setenv("IB_READONLY", "1")
    assert _live.live_readonly() is True
    monkeypatch.delenv("IB_READONLY")
    assert _live.live_readonly() is False


def test_apply_readonly_skips_marks_only_ib_orders() -> None:
    from tests.exchanges.interactive_brokers import _live

    orders = FakeItem("ib_gateway", "ib_orders")
    other = FakeItem("ib_gateway")
    _live.apply_readonly_skips([orders, other], True)
    assert len(orders.added) == 1
    assert "IB_READONLY" in str(orders.added[0].kwargs.get("reason", orders.added[0].args))
    assert orders.added[0].name == "skip"
    assert other.added == []


def test_apply_readonly_skips_noop_when_not_readonly() -> None:
    from tests.exchanges.interactive_brokers import _live

    orders = FakeItem("ib_orders")
    _live.apply_readonly_skips([orders], False)
    assert orders.added == []


def test_conftest_wires_collection_hook() -> None:
    path = IB_TESTS / "conftest.py"
    assert path.is_file(), f"missing {path}"
    spec = importlib.util.spec_from_file_location("ib_conftest_154", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(getattr(module, "pytest_collection_modifyitems", None))
    source = path.read_text(encoding="utf-8")
    assert "apply_readonly_skips" in source


def _gateway_files() -> list[Path]:
    files = []
    for p in sorted(IB_TESTS.glob("test_*.py")):
        if p.name == Path(__file__).name:
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        if _marks_ib_gateway(tree):
            files.append(p)
    return files


def _marks_ib_gateway(tree: ast.Module) -> bool:
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else []
        if any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in targets):
            return any(
                isinstance(sub, ast.Attribute) and sub.attr == "ib_gateway"
                for sub in ast.walk(node)
            )
    return False


def _spec_calls(tree: ast.Module) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and (
            (isinstance(n.func, ast.Name) and n.func.id == "IbConnectionSpec")
            or (isinstance(n.func, ast.Attribute) and n.func.attr == "IbConnectionSpec")
        )
    ]


def test_audit_finds_gateway_files() -> None:
    assert _gateway_files()


@pytest.mark.parametrize("path", _gateway_files(), ids=lambda p: p.name)
def test_gateway_spec_passes_readonly(path: Path) -> None:
    calls = _spec_calls(ast.parse(path.read_text(encoding="utf-8")))
    assert calls, f"{path.name} builds no IbConnectionSpec"
    for call in calls:
        assert any(k.arg == "readonly" for k in call.keywords), (
            f"{path.name}:{call.lineno} IbConnectionSpec( lacks readonly="
        )
