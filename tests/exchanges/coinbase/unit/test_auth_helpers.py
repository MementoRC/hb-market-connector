"""Tests for market_connector.exchanges.coinbase._auth_helpers — Stage 3 Task 15."""

from __future__ import annotations

import pytest

from market_connector.exchanges.coinbase._auth_helpers import _try_normalize_pem


class TestTryNormalizePem:
    def test_valid_multiline_pem_returns_pem(self, ec_private_pem: str) -> None:
        result = _try_normalize_pem(ec_private_pem)
        assert result is not None
        assert result.startswith("-----BEGIN EC PRIVATE KEY-----")
        assert result.endswith("-----END EC PRIVATE KEY-----")

    def test_pem_with_literal_backslash_n_escapes(self, ec_private_pem: str) -> None:
        """PEM with literal \\n instead of real newlines should be normalized."""
        collapsed = ec_private_pem.replace("\n", "\\n")
        result = _try_normalize_pem(collapsed)
        assert result is not None
        assert "-----BEGIN EC PRIVATE KEY-----" in result

    def test_raw_base64_body_returns_pem(self, ec_private_b64: str) -> None:
        result = _try_normalize_pem(ec_private_b64)
        assert result is not None
        assert "-----BEGIN EC PRIVATE KEY-----" in result

    def test_non_pem_hmac_secret_returns_none(self) -> None:
        result = _try_normalize_pem("raw_hmac_secret_not_pem")
        assert result is None

    def test_empty_string_returns_none(self) -> None:
        result = _try_normalize_pem("")
        assert result is None

    def test_garbage_returns_none(self) -> None:
        result = _try_normalize_pem("not!!valid!!base64!!@#$%")
        assert result is None
