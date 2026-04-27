"""Meta-tests for SignerConformance suite (Task 11).

Positive case: known-good HMAC spec + fixture request → suite.run() passes.
Negative case: wrong expected sig → suite.run() raises AssertionError.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest

from market_connector.auth.declarative import DeclarativeRestSigner
from market_connector.testing.contract import SignerConformance
from market_connector.testing.spec_fixtures import KNOWN_HMAC_REQUEST, KNOWN_HMAC_SPEC


def _make_signer() -> DeclarativeRestSigner:
    return DeclarativeRestSigner.from_spec(
        KNOWN_HMAC_SPEC,
        api_key="test-api-key",
        secret="test-secret",
        _fixed_ts="1000000000",
        _fixed_nonce="",
    )


def _expected_sig() -> str:
    """Compute the expected HMAC-SHA256 hex signature for the fixture inputs."""
    # template: "{ts}{method}{path}" with ts=1000000000, method=GET, path=/v1/ping
    sig_input = "1000000000GET/v1/ping"
    return hmac.new(b"test-secret", sig_input.encode(), hashlib.sha256).hexdigest()


def test_signer_conformance_passes_with_correct_expected_sig() -> None:
    """SignerConformance.run() must not raise when expected output matches."""
    signer = _make_signer()
    expected = {"headers": {"X-Signature": _expected_sig()}}
    suite = SignerConformance(
        signer=signer, fixture_request=KNOWN_HMAC_REQUEST, expected_output=expected
    )
    suite.run()


def test_signer_conformance_fails_with_wrong_expected_sig() -> None:
    """SignerConformance.run() must raise AssertionError when expected sig is wrong."""
    signer = _make_signer()
    expected = {"headers": {"X-Signature": "WRONG_SIGNATURE_VALUE"}}
    with pytest.raises(AssertionError):
        SignerConformance(
            signer=signer, fixture_request=KNOWN_HMAC_REQUEST, expected_output=expected
        ).run()
