"""Tests for IbConnectionSpec dataclass."""

from __future__ import annotations

import pytest

from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec


class TestIbConnectionSpec:
    def test_paper_defaults(self):
        spec = IbConnectionSpec()
        assert spec.host == "127.0.0.1"
        assert spec.port == 4002  # paper IB Gateway
        assert spec.client_id == 1
        assert spec.account_id is None
        assert spec.paper is True

    def test_live_construction(self):
        spec = IbConnectionSpec(
            host="ibgw.internal",
            port=4001,
            client_id=42,
            account_id="DU1234567",
            paper=False,
        )
        assert spec.port == 4001
        assert spec.account_id == "DU1234567"
        assert spec.paper is False

    def test_frozen(self):
        spec = IbConnectionSpec()
        with pytest.raises((AttributeError, TypeError)):
            spec.host = "other"  # type: ignore[misc]

    def test_paper_port_validation(self):
        # Convention: when paper=True, port should default to 4002 and live port 4001.
        # No hard validation -- just confirm defaults match doc.
        paper = IbConnectionSpec(paper=True)
        live = IbConnectionSpec(port=4001, paper=False)
        assert paper.port == 4002
        assert live.port == 4001
