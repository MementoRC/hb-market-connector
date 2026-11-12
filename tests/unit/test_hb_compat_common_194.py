"""Acceptance tests for issue #194: hb_compat.common must source enums from data_type_primitives.

Today market_connector/hb_compat/common.py imports OrderType/TradeType from
hummingbot.core.data_type.common. The fix re-exports them from
data_type_primitives.common instead, so the module must import without hummingbot,
expose the same objects, and drop its known-violation allowlist entries.
"""

import ast
import importlib
import importlib.util
import sys
import tomllib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COMMON_PATH = _REPO_ROOT / "market_connector" / "hb_compat" / "common.py"
_COMMON_MODULE = "market_connector.hb_compat.common"
_BOUNDARY_TEST_PATH = _REPO_ROOT / "tests" / "test_import_boundary.py"
_PYPROJECT_PATH = _REPO_ROOT / "pyproject.toml"


def _is_hummingbot(name: str) -> bool:
    return name == "hummingbot" or name.startswith("hummingbot.")


@pytest.mark.unit
def test_import_succeeds_without_hummingbot(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(sys.modules):
        if _is_hummingbot(key):
            monkeypatch.delitem(sys.modules, key)
    # None in sys.modules makes any import of hummingbot (or a child) raise ImportError,
    # so hummingbot is guaranteed absent even if it is installed in the environment.
    monkeypatch.setitem(sys.modules, "hummingbot", None)
    monkeypatch.delitem(sys.modules, _COMMON_MODULE, raising=False)

    module = importlib.import_module(_COMMON_MODULE)

    assert module.__name__ == _COMMON_MODULE


@pytest.mark.unit
def test_enums_are_identical_to_data_type_primitives() -> None:
    common = importlib.import_module(_COMMON_MODULE)
    dtp_common = importlib.import_module("data_type_primitives.common")

    assert common.OrderType is dtp_common.OrderType
    assert common.TradeType is dtp_common.TradeType


@pytest.mark.unit
def test_all_exports_exactly_order_and_trade_type() -> None:
    common = importlib.import_module(_COMMON_MODULE)

    assert set(common.__all__) == {"OrderType", "TradeType"}


@pytest.mark.unit
def test_source_has_no_static_hummingbot_import() -> None:
    tree = ast.parse(_COMMON_PATH.read_text(encoding="utf-8"), filename=str(_COMMON_PATH))
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            violations.extend(
                f"line {node.lineno}: import {alias.name}"
                for alias in node.names
                if _is_hummingbot(alias.name)
            )
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module
            and _is_hummingbot(node.module)
        ):
            violations.append(f"line {node.lineno}: from {node.module} import ...")

    assert violations == []


@pytest.mark.unit
def test_import_boundary_allowlist_has_no_194_entry() -> None:
    spec = importlib.util.spec_from_file_location(
        "test_import_boundary_194_probe", _BOUNDARY_TEST_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    boundary = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(boundary)

    markers = ("hb_compat/common.py", "#194", "/issues/194")
    hits = [
        f"{key} -> {value}"
        for key, value in boundary._KNOWN_VIOLATIONS.items()
        if any(marker in f"{key} {value}" for marker in markers)
    ]

    assert hits == []


@pytest.mark.unit
def test_importlinter_contracts_have_no_hb_compat_common_ignore() -> None:
    with _PYPROJECT_PATH.open("rb") as fh:
        data = tomllib.load(fh)

    contracts = data["tool"]["importlinter"]["contracts"]
    offending = [
        f"{contract.get('name', '<unnamed>')}: {entry}"
        for contract in contracts
        for entry in contract.get("ignore_imports", [])
        if "hb_compat.common" in entry
    ]

    assert offending == []
