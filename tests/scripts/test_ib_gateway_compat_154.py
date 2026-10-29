"""Red acceptance tests: compat script and compose support a read-only Gateway (issue #154)."""

from __future__ import annotations

import importlib.util
import inspect
import re
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "ib_gateway_compat.py"
COMPOSE = REPO_ROOT / "ib-gateway-compose.yml"
README = REPO_ROOT / "market_connector" / "exchanges" / "interactive_brokers" / "README.md"


def _load() -> Any:
    assert SCRIPT.is_file(), f"missing {SCRIPT}"
    spec = importlib.util.spec_from_file_location("ib_gateway_compat", SCRIPT)
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


def _env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for key in ("IB_GATEWAY_HOST", "IB_GATEWAY_PORT", "IB_CLIENT_ID", "IB_READONLY"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


class TestConnectionSpec:
    def test_defaults(self, mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        env = {"IB_GATEWAY_HOST": "10.0.0.5", "IB_GATEWAY_PORT": "14003"}
        _env(monkeypatch, env)
        spec = mod._connection_spec(env)
        assert (spec.host, spec.port) == ("10.0.0.5", 14003)
        assert spec.client_id == 958
        assert spec.paper is True
        assert spec.readonly is False

    def test_client_id_from_env(self, mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        env = {"IB_GATEWAY_HOST": "h", "IB_CLIENT_ID": "77"}
        _env(monkeypatch, env)
        assert mod._connection_spec(env).client_id == 77

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("1", True),
            ("true", True),
            ("YES", True),
            ("on", True),
            ("", False),
            ("0", False),
            ("false", False),
            ("no", False),
        ],
    )
    def test_readonly_rules(
        self, mod: Any, monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
    ) -> None:
        env = {"IB_GATEWAY_HOST": "h", "IB_READONLY": value}
        _env(monkeypatch, env)
        assert mod._connection_spec(env).readonly is expected

    def test_server_info_uses_connection_spec(self, mod: Any) -> None:
        assert "_connection_spec" in inspect.getsource(mod._server_info)


def _compose_env(service: str) -> str:
    text = COMPOSE.read_text(encoding="utf-8")
    try:
        import yaml
    except ImportError:
        block = re.search(rf"^  {re.escape(service)}:\n(.*?)(?=^  \S|\Z)", text, re.M | re.S)
        assert block, f"service {service} not found"
        m = re.search(r"READ_ONLY_API\W+\s*[\"']?(\w+)[\"']?", block.group(1))
        assert m, f"READ_ONLY_API missing for {service}"
        return m.group(1)
    environment = yaml.safe_load(text)["services"][service]["environment"]
    if isinstance(environment, list):
        environment = dict(item.split("=", 1) for item in environment)
    return str(environment["READ_ONLY_API"])


def test_compose_latest_is_read_only() -> None:
    assert _compose_env("ib-gateway-latest") == "yes"


def test_compose_stable_stays_writable() -> None:
    assert _compose_env("ib-gateway") == "no"


def test_readme_latest_command_sets_ib_readonly() -> None:
    text = README.read_text(encoding="utf-8")
    cmd = [line for line in text.splitlines() if "--channel latest" in line]
    assert cmd, "no latest-channel command line"
    assert any("IB_READONLY=1" in line for line in cmd)


def test_readme_no_longer_claims_latest_is_writable() -> None:
    text = README.read_text(encoding="utf-8")
    assert not re.search(r"latest[^\n]*READ_ONLY_API=no", text, re.I)
    assert "READ_ONLY_API=no`, because the connector cannot yet" not in text
