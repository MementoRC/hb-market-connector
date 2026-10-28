"""Static audit of live-gated IB test markers (issue #144).

Live files must declare ib_gateway, and ib_orders too if they place or cancel orders.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

IB_TESTS = Path(__file__).resolve().parent
ORDER_CALLS = frozenset({"place_order", "cancel_order", "cancel_all", "placeOrder", "cancelOrder"})


def _live_files() -> list[Path]:
    return sorted(
        p
        for p in IB_TESTS.glob("test_*.py")
        if p.name != Path(__file__).name and "IB_GATEWAY_HOST" in p.read_text(encoding="utf-8")
    )


def _declared_markers(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else []
        if any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in targets):
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Attribute)
                    and isinstance(sub.value, ast.Attribute)
                    and sub.value.attr == "mark"
                ):
                    names.add(sub.attr)
    return names


def _calls_orders(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in ORDER_CALLS:
                return True
    return False


def test_audit_finds_live_files() -> None:
    assert _live_files(), "no live-gated IB test files found"


@pytest.mark.parametrize("path", _live_files(), ids=lambda p: p.name)
def test_live_file_declares_ib_gateway(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assert "ib_gateway" in _declared_markers(tree), f"{path.name} lacks pytestmark ib_gateway"


@pytest.mark.parametrize("path", _live_files(), ids=lambda p: p.name)
def test_order_placing_file_declares_ib_orders(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    if _calls_orders(tree):
        assert "ib_orders" in _declared_markers(tree), f"{path.name} lacks pytestmark ib_orders"


def test_some_live_file_declares_ib_orders() -> None:
    declared = [
        p.name
        for p in _live_files()
        if "ib_orders" in _declared_markers(ast.parse(p.read_text(encoding="utf-8")))
    ]
    assert declared, "no live file declares ib_orders"
