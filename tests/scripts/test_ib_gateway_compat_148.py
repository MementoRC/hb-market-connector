"""Red acceptance tests for Gateway build auto-detection (issue #148)."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "ib_gateway_compat.py"


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


class FakeRun:
    def __init__(self, stdout: str = "", returncode: int = 0, exc: Exception | None = None) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.exc = exc
        self.calls: list[list[str]] = []

    def __call__(self, args: Any, *_a: Any, **_kw: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        if self.exc is not None:
            raise self.exc
        return subprocess.CompletedProcess(args, self.returncode, stdout=self.stdout, stderr="")


def _format_template(args: list[str]) -> str:
    for i, arg in enumerate(args):
        if arg == "--format" and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--format="):
            return arg.split("=", 1)[1]
    raise AssertionError(f"no --format argument in {args}")


def test_detect_builds_filtered_docker_inspect_command(mod: Any) -> None:
    fake = FakeRun("IB_GATEWAY_VERSION=10.50.1f\n")
    mod.detect_gateway_build("my-gw", run=fake)
    args = fake.calls[0]
    assert args[0] == "docker"
    assert args[1] == "inspect"
    assert "my-gw" in args
    template = _format_template(args)
    assert "IB_GATEWAY_VERSION" in template
    assert "{{json" not in template
    assert ".Config.Env}}{{println .}}" not in template


def test_detect_returns_version(mod: Any) -> None:
    assert mod.detect_gateway_build("c", run=FakeRun("IB_GATEWAY_VERSION=10.50.1f\n")) == "10.50.1f"


def test_detect_empty_stdout_is_none(mod: Any) -> None:
    assert mod.detect_gateway_build("c", run=FakeRun("")) is None


def test_detect_nonzero_returncode_is_none(mod: Any) -> None:
    assert mod.detect_gateway_build("c", run=FakeRun("IB_GATEWAY_VERSION=1.2\n", 1)) is None


def test_detect_docker_missing_is_none(mod: Any) -> None:
    assert mod.detect_gateway_build("c", run=FakeRun(exc=FileNotFoundError("docker"))) is None


def test_detect_never_leaks_other_env(mod: Any) -> None:
    out = "IB_GATEWAY_VERSION=10.50.1f\nTWS_PASSWORD=secret\n"
    result = mod.detect_gateway_build("c", run=FakeRun(out))
    assert result == "10.50.1f"
    assert "secret" not in result


def test_resolve_cli_wins_without_running(mod: Any) -> None:
    fake = FakeRun("IB_GATEWAY_VERSION=10.50.1f\n")
    env = {"IB_GATEWAY_BUILD": "8.8", "IB_GATEWAY_CONTAINER": "c"}
    assert mod.resolve_gateway_build("9.9", env, run=fake) == "9.9"
    assert fake.calls == []


def test_resolve_env_build_without_running(mod: Any) -> None:
    fake = FakeRun("IB_GATEWAY_VERSION=10.50.1f\n")
    env = {"IB_GATEWAY_BUILD": "8.8", "IB_GATEWAY_CONTAINER": "c"}
    assert mod.resolve_gateway_build(None, env, run=fake) == "8.8"
    assert fake.calls == []


def test_resolve_detects_from_env_container(mod: Any) -> None:
    fake = FakeRun("IB_GATEWAY_VERSION=10.50.1f\n")
    assert mod.resolve_gateway_build(None, {"IB_GATEWAY_CONTAINER": "c"}, run=fake) == "10.50.1f"


def test_resolve_unknown_without_container_or_build(mod: Any) -> None:
    fake = FakeRun("IB_GATEWAY_VERSION=10.50.1f\n")
    assert mod.resolve_gateway_build(None, {}, run=fake) == "unknown"
    assert fake.calls == []


def test_resolve_unknown_when_detection_fails(mod: Any) -> None:
    fake = FakeRun("", 1)
    assert mod.resolve_gateway_build(None, {"IB_GATEWAY_CONTAINER": "c"}, run=fake) == "unknown"


def test_resolve_container_keyword_overrides_env(mod: Any) -> None:
    fake = FakeRun("IB_GATEWAY_VERSION=10.50.1f\n")
    assert mod.resolve_gateway_build(None, {}, run=fake, container="c") == "10.50.1f"
