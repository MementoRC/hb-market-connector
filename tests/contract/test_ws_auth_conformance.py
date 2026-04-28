"""Meta-tests for WsAuthModelConformance suite (Task 11).

Positive case: PassThroughAuth → suite.run() completes without exception.
Negative case: transform_outgoing returning wrong shape → suite.run() raises AssertionError.
"""

from __future__ import annotations

import pytest

from market_connector.testing.contract import WsAuthModelConformance
from market_connector.ws_models.auth_models import PassThroughAuth


@pytest.mark.asyncio
async def test_ws_auth_passthrough_conformance_passes() -> None:
    """WsAuthModelConformance.run() must not raise for PassThroughAuth."""
    model = PassThroughAuth()
    sample_msg = {"op": "subscribe", "args": ["ticker"]}
    suite = WsAuthModelConformance(
        model=model,
        expected_transform_output=sample_msg,  # PassThrough returns msg unchanged
        sample_msg=sample_msg,
    )
    await suite.run()


@pytest.mark.asyncio
async def test_ws_auth_conformance_fails_on_wrong_transform_output() -> None:
    """WsAuthModelConformance.run() raises AssertionError when transform produces wrong output."""
    model = PassThroughAuth()
    sample_msg = {"op": "subscribe", "args": ["ticker"]}
    wrong_expected = {"op": "subscribe", "args": ["orderbook"], "injected": True}
    suite = WsAuthModelConformance(
        model=model,
        expected_transform_output=wrong_expected,
        sample_msg=sample_msg,
    )
    with pytest.raises(AssertionError):
        await suite.run()
