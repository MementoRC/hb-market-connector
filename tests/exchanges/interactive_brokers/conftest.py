"""Skip order-placing live tests when IB_READONLY is set (issue #154)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.exchanges.interactive_brokers._live import apply_readonly_skips, live_readonly

if TYPE_CHECKING:
    import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    # Only IB tests carry the ib_orders marker, so other suites are unaffected.
    apply_readonly_skips(items, live_readonly())
