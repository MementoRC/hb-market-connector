"""Live Gateway compatibility check (issue #144). READ-ONLY: no orders.

Skipped unless IB_GATEWAY_HOST is set. Paper Gateway only (live ports are refused).
"""

from __future__ import annotations

import os

import pytest

if not os.environ.get("IB_GATEWAY_HOST"):
    pytest.skip(
        "Set IB_GATEWAY_HOST to run integration tests against a live IB Gateway",
        allow_module_level=True,
    )

from ib_async.client import Client

from market_connector.exchanges.interactive_brokers.factory import build_ib_gateway
from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec

from ._live import live_endpoint

pytestmark = [pytest.mark.ib_gateway]

_MIN_SERVER_VERSION = 157


async def test_server_info_reports_compat_facts() -> None:
    host, port = live_endpoint()
    spec = IbConnectionSpec(
        host=host,
        port=port,
        client_id=int(os.environ.get("IB_CLIENT_ID", "958")),
        account_id=os.environ.get("IB_PAPER_ACCOUNT"),
        paper=True,
    )
    gateway = build_ib_gateway(spec)
    try:
        await gateway.start()
        info = gateway.server_info
        assert info is not None
        assert info.server_version >= _MIN_SERVER_VERSION
        assert info.max_client_version == Client.MaxClientVersion
        assert info.at_client_ceiling == (info.server_version >= info.max_client_version)
    finally:
        await gateway.stop()
