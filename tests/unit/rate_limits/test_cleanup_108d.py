"""Acceptance tests for hb-market-connector #108 slice (d): runtime limiter cleanup.

Red until slice (d) lands. Targets that do not exist yet (``rate_limits.specs``,
``rate_limit.testing``-backed conformance) are imported inside test functions so this
module always collects.
"""

from __future__ import annotations

import importlib
import pathlib
from typing import TYPE_CHECKING, Any

import pytest

import market_connector.rate_limits as rate_limits
import market_connector.transport as transport

if TYPE_CHECKING:
    from rate_limit import Clock, Sleep

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

REMOVED_MODULES = [
    "market_connector.rate_limits.flat",
    "market_connector.rate_limits.tiered",
    "market_connector.rate_limits.pool",
    "market_connector.transport.token_bucket",
]

REMOVED_NAMES = [
    (rate_limits, "FlatRateLimit"),
    (rate_limits, "TieredRateLimit"),
    (rate_limits, "RateLimit"),
    (transport, "TokenBucket"),
]

SPEC_NAMES = ["PoolSpec", "FlatRateLimitSpec", "TierProfile", "TieredRateLimitSpec"]

SURVIVING_HELPERS = [
    "limiter_from_flat_spec",
    "limiter_from_tiered_spec",
    "pool_to_rate_limit",
    "ensure_keyed_limit",
    "RateLimitExceeded",
]


# ---- G1: removed runtime limiters ---------------------------------------------------


@pytest.mark.parametrize("module_name", REMOVED_MODULES)
def test_removed_runtime_limiter_module_is_not_importable(module_name: str) -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module_name)


@pytest.mark.parametrize(("package", "name"), REMOVED_NAMES)
def test_removed_runtime_limiter_name_is_not_an_attribute(package: Any, name: str) -> None:
    assert not hasattr(package, name)


@pytest.mark.parametrize(("package", "name"), REMOVED_NAMES)
def test_removed_runtime_limiter_name_is_not_in_all(package: Any, name: str) -> None:
    assert name not in package.__all__


# ---- G2: packages and declarative specs survive -------------------------------------


@pytest.mark.parametrize(
    "package_name", ["market_connector.rate_limits", "market_connector.transport"]
)
def test_package_still_imports(package_name: str) -> None:
    assert importlib.import_module(package_name) is not None


@pytest.mark.parametrize("name", SPEC_NAMES)
def test_spec_defined_in_specs_module_is_reexported_identically(name: str) -> None:
    specs = importlib.import_module("market_connector.rate_limits.specs")
    assert getattr(specs, name) is getattr(rate_limits, name)
    assert name in rate_limits.__all__


def test_pool_spec_rejects_zero_capacity() -> None:
    specs = importlib.import_module("market_connector.rate_limits.specs")
    with pytest.raises(ValueError):
        specs.PoolSpec(name="p", capacity=0, refill_rate=1.0)


def test_tiered_spec_empty_keyed_endpoint_pools() -> None:
    specs = importlib.import_module("market_connector.rate_limits.specs")
    spec = specs.TieredRateLimitSpec(public_pools={}, tiers={}, endpoint_pools={})
    assert spec.keyed_endpoint_pools == {}


@pytest.mark.parametrize("name", SURVIVING_HELPERS)
def test_surviving_helper_is_exported(name: str) -> None:
    assert hasattr(rate_limits, name)
    assert name in rate_limits.__all__


# ---- G3: RateLimitConformance delegates to rate_limit.testing -----------------------


def test_conformance_run_passes_for_conforming_limiter() -> None:
    from rate_limit import RateLimit, RateLimiter

    from market_connector.testing import RateLimitConformance

    def make(clock: Clock, sleep: Sleep) -> Any:
        return RateLimiter([RateLimit("a", 3, 1.0)], clock=clock, sleep=sleep).bind("a")

    RateLimitConformance(make, limit=3, interval=1.0).run()


def test_conformance_run_delegates_to_assert_conforms_rest_limiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import market_connector.testing.contract as contract
    from market_connector.testing import RateLimitConformance

    calls: list[tuple[object, dict[str, object]]] = []

    def recorder(make: object, *, limit: int, interval: float) -> None:
        calls.append((make, {"limit": limit, "interval": interval}))

    monkeypatch.setattr(contract, "assert_conforms_rest_limiter", recorder)
    make = object()
    RateLimitConformance(make, limit=5, interval=2.5).run()
    assert calls == [(make, {"limit": 5, "interval": 2.5})]


def test_old_acquire_based_conformance_signature_is_gone() -> None:
    from market_connector.testing import RateLimitConformance

    with pytest.raises(TypeError):
        RateLimitConformance(
            rate_limit=object(), request_stream=[], expected_max_duration_seconds=1.0
        )


# ---- G4: release hygiene ------------------------------------------------------------


def test_about_version_is_0_2_0() -> None:
    about = importlib.import_module("market_connector.__about__")
    assert about.__version__ == "0.2.0"


def test_changelog_exists_and_documents_removals() -> None:
    changelog = REPO_ROOT / "CHANGELOG.md"
    assert changelog.is_file(), "CHANGELOG.md must exist at the package root"
    text = changelog.read_text(encoding="utf-8")
    assert "0.2.0" in text
    lines = [line.lower() for line in text.splitlines()]
    for name in ("FlatRateLimit", "TieredRateLimit", "TokenBucket"):
        assert any(name.lower() in line and "removed" in line for line in lines), (
            f"CHANGELOG.md must mention {name} as removed"
        )
