"""Tests for pre-built auth scheme constants (Task 6, spec §6.8).

Each scheme is a module-level HmacSigningSpec constant covering a CEX family.
Tests verify:
  - the constant is an HmacSigningSpec instance
  - recipe.template matches the expected pattern for that family
  - output injection shape (qs_inject vs headers)
  - algorithm / output_encoding for the family
  - dataclasses.replace works to customise header/qs names (exchange override pattern)
"""

from __future__ import annotations

import dataclasses

import pytest

from market_connector.auth.schemes import (
    HMAC_GATE_IO_MULTILINE,
    HMAC_QS_SORTED_TS,
    HMAC_TS_METHOD_PATH_BODY_BASE64,
    HMAC_TS_METHOD_PATH_BODY_HEX,
)
from market_connector.auth.spec import (
    HmacSigningSpec,
    SigAlgorithm,
    SigEncoding,
)


class TestHmacQsSortedTs:
    """HMAC_QS_SORTED_TS — Binance family (7 connectors)."""

    def test_is_hmac_signing_spec(self) -> None:
        assert isinstance(HMAC_QS_SORTED_TS, HmacSigningSpec)

    def test_recipe_template_contains_qs_sorted(self) -> None:
        assert "{qs_sorted}" in HMAC_QS_SORTED_TS.recipe.template

    def test_signature_goes_into_qs_inject(self) -> None:
        assert HMAC_QS_SORTED_TS.output.qs_inject is not None
        assert HMAC_QS_SORTED_TS.output.headers == {}

    def test_algorithm_is_sha256(self) -> None:
        assert HMAC_QS_SORTED_TS.recipe.algorithm == SigAlgorithm.HMAC_SHA256

    def test_output_encoding_is_hex(self) -> None:
        assert HMAC_QS_SORTED_TS.recipe.output_encoding == SigEncoding.HEX

    def test_no_body_hash(self) -> None:
        assert HMAC_QS_SORTED_TS.recipe.body_hash is None

    def test_exchange_override_via_replace(self) -> None:
        """Exchanges customise qs_inject key name via dataclasses.replace."""
        custom_output = dataclasses.replace(
            HMAC_QS_SORTED_TS.output,
            qs_inject={"mySignature": "{sig}"},
        )
        custom = dataclasses.replace(HMAC_QS_SORTED_TS, output=custom_output)
        assert isinstance(custom, HmacSigningSpec)
        assert custom.output.qs_inject == {"mySignature": "{sig}"}


class TestHmacTsMethodPathBodyHex:
    """HMAC_TS_METHOD_PATH_BODY_HEX — OKX-family HEX output (15 connectors)."""

    def test_is_hmac_signing_spec(self) -> None:
        assert isinstance(HMAC_TS_METHOD_PATH_BODY_HEX, HmacSigningSpec)

    def test_recipe_template_matches_family(self) -> None:
        tmpl = HMAC_TS_METHOD_PATH_BODY_HEX.recipe.template
        assert "{ts}" in tmpl
        assert "{method}" in tmpl
        assert "{path}" in tmpl
        assert "{body}" in tmpl

    def test_signature_goes_into_headers(self) -> None:
        assert HMAC_TS_METHOD_PATH_BODY_HEX.output.qs_inject is None
        assert HMAC_TS_METHOD_PATH_BODY_HEX.output.headers != {}

    def test_output_encoding_is_hex(self) -> None:
        assert HMAC_TS_METHOD_PATH_BODY_HEX.recipe.output_encoding == SigEncoding.HEX

    def test_algorithm_is_sha256(self) -> None:
        assert HMAC_TS_METHOD_PATH_BODY_HEX.recipe.algorithm == SigAlgorithm.HMAC_SHA256

    def test_no_body_hash(self) -> None:
        assert HMAC_TS_METHOD_PATH_BODY_HEX.recipe.body_hash is None

    def test_exchange_override_adds_extra_header(self) -> None:
        """Exchange adds its own header names via replace on output.headers."""
        custom_headers = dict(HMAC_TS_METHOD_PATH_BODY_HEX.output.headers)
        custom_headers["X-CUSTOM-SIGN"] = "{sig}"
        custom_output = dataclasses.replace(
            HMAC_TS_METHOD_PATH_BODY_HEX.output,
            headers=custom_headers,
        )
        custom = dataclasses.replace(HMAC_TS_METHOD_PATH_BODY_HEX, output=custom_output)
        assert "X-CUSTOM-SIGN" in custom.output.headers


class TestHmacTsMethodPathBodyBase64:
    """HMAC_TS_METHOD_PATH_BODY_BASE64 — OKX / Bitget base64 variant."""

    def test_is_hmac_signing_spec(self) -> None:
        assert isinstance(HMAC_TS_METHOD_PATH_BODY_BASE64, HmacSigningSpec)

    def test_recipe_template_matches_family(self) -> None:
        tmpl = HMAC_TS_METHOD_PATH_BODY_BASE64.recipe.template
        assert "{ts}" in tmpl
        assert "{method}" in tmpl
        assert "{path}" in tmpl
        assert "{body}" in tmpl

    def test_output_encoding_is_base64(self) -> None:
        assert HMAC_TS_METHOD_PATH_BODY_BASE64.recipe.output_encoding == SigEncoding.BASE64

    def test_signature_goes_into_headers(self) -> None:
        assert HMAC_TS_METHOD_PATH_BODY_BASE64.output.qs_inject is None
        assert HMAC_TS_METHOD_PATH_BODY_BASE64.output.headers != {}

    def test_algorithm_is_sha256(self) -> None:
        assert HMAC_TS_METHOD_PATH_BODY_BASE64.recipe.algorithm == SigAlgorithm.HMAC_SHA256

    def test_template_same_as_hex_variant(self) -> None:
        """Both variants share the same template — only encoding differs."""
        assert (
            HMAC_TS_METHOD_PATH_BODY_BASE64.recipe.template
            == HMAC_TS_METHOD_PATH_BODY_HEX.recipe.template
        )


class TestHmacGateIoMultiline:
    """HMAC_GATE_IO_MULTILINE — Gate.io SHA-512 newline-separated template."""

    def test_is_hmac_signing_spec(self) -> None:
        assert isinstance(HMAC_GATE_IO_MULTILINE, HmacSigningSpec)

    def test_recipe_template_contains_newlines(self) -> None:
        assert "\n" in HMAC_GATE_IO_MULTILINE.recipe.template

    def test_algorithm_is_sha512(self) -> None:
        assert HMAC_GATE_IO_MULTILINE.recipe.algorithm == SigAlgorithm.HMAC_SHA512

    def test_output_encoding_is_hex(self) -> None:
        assert HMAC_GATE_IO_MULTILINE.recipe.output_encoding == SigEncoding.HEX

    def test_signature_goes_into_headers(self) -> None:
        assert HMAC_GATE_IO_MULTILINE.output.qs_inject is None
        assert HMAC_GATE_IO_MULTILINE.output.headers != {}

    def test_no_body_hash(self) -> None:
        assert HMAC_GATE_IO_MULTILINE.recipe.body_hash is None

    def test_template_contains_method_and_path(self) -> None:
        tmpl = HMAC_GATE_IO_MULTILINE.recipe.template
        assert "{method}" in tmpl
        assert "{path}" in tmpl


class TestSchemesAreImmutable:
    """Module-level constants must be frozen — no mutation in place."""

    def test_qs_sorted_is_frozen(self) -> None:
        with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
            HMAC_QS_SORTED_TS.recipe = HMAC_QS_SORTED_TS.recipe  # type: ignore[misc]

    def test_hex_is_frozen(self) -> None:
        with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
            HMAC_TS_METHOD_PATH_BODY_HEX.recipe = HMAC_TS_METHOD_PATH_BODY_HEX.recipe  # type: ignore[misc]

    def test_base64_is_frozen(self) -> None:
        with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
            HMAC_TS_METHOD_PATH_BODY_BASE64.recipe = HMAC_TS_METHOD_PATH_BODY_BASE64.recipe  # type: ignore[misc]

    def test_gate_io_is_frozen(self) -> None:
        with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
            HMAC_GATE_IO_MULTILINE.recipe = HMAC_GATE_IO_MULTILINE.recipe  # type: ignore[misc]
