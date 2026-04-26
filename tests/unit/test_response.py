"""Tests for the Response wrapper dataclass."""

import httpx
import pytest
from pydantic import BaseModel

from market_connector.transport.response import Response


class _Schema(BaseModel):
    foo: str


class TestResponseConstruction:
    def test_minimal(self) -> None:
        r: Response = Response(raw={"foo": "bar"})
        assert r.raw == {"foo": "bar"}
        assert r.status_code == 200
        assert isinstance(r.headers, httpx.Headers)

    def test_full(self) -> None:
        h = httpx.Headers({"X-Cursor": "abc"})
        r = Response(
            raw=[{"a": 1}],
            status_code=201,
            headers=h,
            _endpoint="list_x",
            _response_type=_Schema,
        )
        assert r.raw == [{"a": 1}]
        assert r.status_code == 201
        assert r.headers["x-cursor"] == "abc"  # case-insensitive
        assert r._endpoint == "list_x"
        assert r._response_type is _Schema

    def test_invariant_raw_none_with_typed_raises(self) -> None:
        with pytest.raises(ValueError, match="empty body but response_type"):
            Response(raw=None, _endpoint="x", _response_type=_Schema)

    def test_raw_none_no_type_is_ok(self) -> None:
        r = Response(raw=None)
        assert r.raw is None

    def test_repr_excludes_raw_and_internals(self) -> None:
        r = Response(
            raw={"big": "payload"},
            _endpoint="list_x",
            _response_type=_Schema,
        )
        rep = repr(r)
        assert "big" not in rep
        assert "list_x" not in rep
        assert "_Schema" not in rep
        assert "_cached" not in rep
