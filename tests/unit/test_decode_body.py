"""Tests for _decode_body content-type aware JSON decoder."""

import json as _json

import httpx
import pytest

from market_connector.transport.rest_base import _decode_body


def _make_response(
    content: bytes,
    content_type: str | None = "application/json",
    status: int = 200,
) -> httpx.Response:
    headers = {}
    if content_type is not None:
        headers["content-type"] = content_type
    return httpx.Response(status_code=status, content=content, headers=headers)


class TestDecodeBody:
    def test_empty_body_returns_none(self) -> None:
        r = _make_response(b"", status=204, content_type=None)
        assert _decode_body(r) is None

    def test_text_plain_returns_none(self) -> None:
        r = _make_response(b"OK", content_type="text/plain")
        assert _decode_body(r) is None

    def test_application_json_returns_parsed(self) -> None:
        r = _make_response(b'{"foo": "bar"}', content_type="application/json")
        assert _decode_body(r) == {"foo": "bar"}

    def test_application_json_with_charset_parses(self) -> None:
        r = _make_response(b'{"foo": "bar"}', content_type="application/json; charset=utf-8")
        assert _decode_body(r) == {"foo": "bar"}

    def test_application_json_list_returns_list(self) -> None:
        r = _make_response(b'[{"a": 1}, {"b": 2}]', content_type="application/json")
        assert _decode_body(r) == [{"a": 1}, {"b": 2}]

    def test_malformed_json_propagates_error(self) -> None:
        r = _make_response(b"{not json", content_type="application/json")
        with pytest.raises(_json.JSONDecodeError):
            _decode_body(r)

    def test_missing_content_type_returns_none(self) -> None:
        r = _make_response(b'{"foo": "bar"}', content_type=None)
        assert _decode_body(r) is None

    def test_application_problem_json_returns_none(self) -> None:
        r = _make_response(b'{"detail": "x"}', content_type="application/problem+json")
        assert _decode_body(r) is None

    def test_application_json_patch_json_returns_none(self) -> None:
        r = _make_response(b"[{}]", content_type="application/json-patch+json")
        assert _decode_body(r) is None

    def test_uppercase_application_json_parses(self) -> None:
        r = _make_response(b'{"foo": "bar"}', content_type="Application/JSON")
        assert _decode_body(r) == {"foo": "bar"}
