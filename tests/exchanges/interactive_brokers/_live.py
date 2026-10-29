"""Shared host/port resolution and paper-only guard for live IB Gateway tests.

Host comes from ``IB_GATEWAY_HOST`` (default ``127.0.0.1``), port from
``IB_GATEWAY_PORT`` (default ``4002``, the paper IB Gateway port).

The guard refuses to run against the well-known LIVE-trading defaults (4001 for
IB Gateway, 7496 for TWS). It cannot detect a live Gateway on a non-default
port, so IB_GATEWAY_PORT must point at a paper Gateway.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

READONLY_TRUTHY = frozenset({"1", "true", "yes", "on"})
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4002
LIVE_TRADING_PORTS = frozenset({4001, 7496})


def live_host() -> str:
    return os.environ.get("IB_GATEWAY_HOST", DEFAULT_HOST)


def live_port() -> int:
    """Return the configured port; fail (before connecting) if it is a live-trading port."""
    raw = os.environ.get("IB_GATEWAY_PORT", str(DEFAULT_PORT))
    try:
        port = int(raw)
    except ValueError:
        pytest.fail(f"IB_GATEWAY_PORT must be an integer, got {raw!r}")
    if port in LIVE_TRADING_PORTS:
        pytest.fail(
            f"IB_GATEWAY_PORT={port} is a LIVE-trading default port (4001 IB Gateway live, "
            "7496 TWS live). These tests place real orders and must only run against a paper "
            "Gateway (default 4002). Point IB_GATEWAY_PORT at a paper Gateway."
        )
    return port


def live_endpoint() -> tuple[str, int]:
    return live_host(), live_port()


def live_readonly(env: Mapping[str, str] | None = None) -> bool:
    """True when IB_READONLY is set to 1/true/yes/on (case-insensitive): connect read-only."""
    source = os.environ if env is None else env
    return source.get("IB_READONLY", "").strip().lower() in READONLY_TRUTHY


def apply_readonly_skips(items: Iterable[Any], readonly: bool) -> None:
    """Skip order-placing (``ib_orders``) tests when connected to a read-only Gateway."""
    if not readonly:
        return
    marker = pytest.mark.skip(reason="order placement needs a writable API; IB_READONLY is set")
    for item in items:
        if item.get_closest_marker("ib_orders") is not None:
            item.add_marker(marker)
