"""Tests for the Response wrapper dataclass."""

import httpx
import pytest
from pydantic import BaseModel, RootModel

from market_connector.transport.response import Response
from market_connector.transport.errors import MarketConnectorParseError


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


class TestResponseParse:
    def test_returns_raw_when_no_response_type(self) -> None:
        raw = {"foo": "bar"}
        r = Response(raw=raw)
        assert r.parse() is raw

    def test_returns_typed_model_when_set(self) -> None:
        r = Response(raw={"foo": "x"}, _response_type=_Schema)
        parsed = r.parse()
        assert isinstance(parsed, _Schema)
        assert parsed.foo == "x"

    def test_caches_result_on_second_call(self) -> None:
        r = Response(raw={"foo": "x"}, _response_type=_Schema)
        first = r.parse()
        second = r.parse()
        assert first is second  # identity preserved

    def test_caches_raw_when_no_response_type(self) -> None:
        raw = [{"a": 1}, {"b": 2}]
        r = Response(raw=raw)
        first = r.parse()
        second = r.parse()
        assert first is second is raw

    def test_raises_market_connector_parse_error_on_validation_failure(self) -> None:
        # foo is required str, but provided int
        r = Response(raw={"foo": 123}, _endpoint="list_x", _response_type=_Schema)
        with pytest.raises(MarketConnectorParseError) as exc_info:
            r.parse()
        err = exc_info.value
        assert err.endpoint == "list_x"
        assert err.raw == {"foo": 123}
        assert err.__cause__ is err.original

    def test_returns_none_when_raw_none_and_no_type(self) -> None:
        r = Response(raw=None)
        assert r.parse() is None

    def test_repr_after_parse_still_excludes_cached(self) -> None:
        r = Response(raw={"foo": "x"}, _response_type=_Schema)
        r.parse()  # populates _cached
        rep = repr(r)
        assert "_cached" not in rep
        assert "_Schema" not in rep  # _response_type still excluded

    def test_parse_with_root_model_for_list_endpoint(self) -> None:
        """List-shaped JSON requires RootModel[list[Item]] — not plain BaseModel."""

        class _Item(BaseModel):
            id: str

        class _ItemList(RootModel[list[_Item]]):
            pass

        r = Response(raw=[{"id": "a"}, {"id": "b"}], _response_type=_ItemList)
        parsed = r.parse()
        assert isinstance(parsed, _ItemList)
        assert parsed.root[0].id == "a"
        assert parsed.root[1].id == "b"
