"""Tests for PassThroughSigner (no-op signer for process-managed auth)."""

from __future__ import annotations

import pytest

from market_connector.auth.passthrough import PassThroughSigner
from market_connector.auth.protocols import Request, Signer


class TestPassThroughSigner:
    def test_satisfies_signer_protocol(self):
        s = PassThroughSigner()
        assert isinstance(s, Signer)

    @pytest.mark.asyncio
    async def test_sign_returns_request_unchanged(self):
        s = PassThroughSigner()
        req = Request(
            method="GET",
            url="https://example/test",
            path="/test",
            headers={"X-Test": "value"},
            body=None,
            qs_params={"k": "v"},
        )
        signed = await s.sign(req)
        assert signed is req

    @pytest.mark.asyncio
    async def test_sign_with_body(self):
        s = PassThroughSigner()
        req = Request(
            method="POST",
            url="https://example/order",
            path="/order",
            headers={},
            body=b'{"qty": 10}',
            qs_params={},
        )
        signed = await s.sign(req)
        assert signed.body == b'{"qty": 10}'
