"""Kraken ``cl_ord_id`` primitives (#255), RED acceptance tests.

Pins: ``place_order(cl_ord_id=)``, ``OrderInfo.cl_ord_id``, ``get_open_orders`` /
``get_closed_orders(cl_ord_id=)``, ``cancel_order(cl_ord_id=)`` and the #248 codec round-trip.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest

from market_connector.exceptions import ExchangeUnavailableError, OrderOutcomeUnknownError
from market_connector.exchanges.kraken.linked_order_codec import KrakenClOrdIdLinkCodec
from market_connector.exchanges.kraken.schemas.rest import OrderInfo
from market_connector.primitives import OrderType, TradeType
from market_connector.testing.mock_transport import MockRestClient
from tests.exchanges.kraken.unit.test_orders_mixin import _order_info_raw, _TestableOrders

_ADD_OK = {"error": [], "result": {"txid": ["OABCDE-12345-FGHIJK"], "descr": {"order": "t"}}}
_CANCEL_OK = {"error": [], "result": {"count": 1}}
_CID = "mcl-1-0"
_LOST = [
    httpx.ReadTimeout("response lost"),
    httpx.ConnectError("connection reset"),
    ExchangeUnavailableError("transport error on add_order"),
]
_LOST_IDS = ["timeout", "transport", "unavailable"]


class _RecordingRest(MockRestClient):
    """MockRestClient that records request bodies and can raise instead of replying."""

    def __init__(self) -> None:
        super().__init__()
        self.bodies: list[tuple[str, dict[str, Any]]] = []
        self.raises: Exception | None = None

    async def request(self, endpoint_name: str, *args: Any, **kwargs: Any) -> Any:
        self.bodies.append((endpoint_name, dict(kwargs.get("data") or {})))
        if self.raises is not None:
            raise self.raises
        return await super().request(endpoint_name, data=kwargs.get("data"))


def _mixin(endpoint: str, reply: dict[str, Any]) -> tuple[_TestableOrders, _RecordingRest]:
    rest = _RecordingRest()
    rest.register(endpoint, reply)
    return _TestableOrders(rest), rest


async def _place(mixin: _TestableOrders, **kwargs: Any) -> str:
    return await mixin.place_order(  # type: ignore[no-any-return]
        "XBTUSD", OrderType.LIMIT, TradeType.BUY, Decimal("0.01"), Decimal("100.5"), **kwargs
    )


# ---------------------------------------------------------------------------
# place_order
# ---------------------------------------------------------------------------


class TestPlaceOrderClOrdId:
    async def test_body_has_cl_ord_id_and_no_userref(self) -> None:
        mixin, rest = _mixin("add_order", _ADD_OK)

        txid = await _place(mixin, cl_ord_id=_CID)

        assert txid == "OABCDE-12345-FGHIJK"
        body = rest.bodies[0][1]
        assert body["cl_ord_id"] == _CID
        assert "userref" not in body

    async def test_both_userref_and_cl_ord_id_rejected_before_request(self) -> None:
        mixin, rest = _mixin("add_order", _ADD_OK)

        with pytest.raises(ValueError):
            await _place(mixin, userref=5, cl_ord_id=_CID)

        assert rest.bodies == []

    @pytest.mark.parametrize(
        "bad",
        ["", "x" * 19, "mcl-100000000000-15-extra", "clé-1-0", "ｍcl-1-0"],
        ids=["empty", "19-chars", "long", "non-ascii-accent", "non-ascii-fullwidth"],
    )
    async def test_invalid_cl_ord_id_rejected_before_request(self, bad: str) -> None:
        mixin, rest = _mixin("add_order", _ADD_OK)

        with pytest.raises(ValueError):
            await _place(mixin, cl_ord_id=bad)

        assert rest.bodies == []

    @pytest.mark.parametrize(
        "good",
        ["mcl-1-0", "mcl-99999999999-15", str(uuid.uuid4()), uuid.uuid4().hex],
        ids=["short", "exactly-18", "uuid-dashed", "uuid-hex"],
    )
    async def test_valid_cl_ord_id_accepted(self, good: str) -> None:
        mixin, rest = _mixin("add_order", _ADD_OK)

        await _place(mixin, cl_ord_id=good)

        assert rest.bodies[0][1]["cl_ord_id"] == good

    @pytest.mark.parametrize("exc", _LOST, ids=_LOST_IDS)
    async def test_lost_reply_raises_outcome_unknown_with_client_order_id(
        self, exc: Exception
    ) -> None:
        mixin, rest = _mixin("add_order", _ADD_OK)
        rest.raises = exc

        with pytest.raises(OrderOutcomeUnknownError) as info:
            await _place(mixin, cl_ord_id=_CID)

        assert info.value.client_order_id == _CID

    async def test_explicit_api_error_is_not_outcome_unknown(self) -> None:
        mixin, rest = _mixin("add_order", {"error": ["EOrder:Insufficient funds"], "result": {}})

        with pytest.raises(Exception) as info:  # noqa: PT011
            await _place(mixin, cl_ord_id=_CID)

        assert rest.bodies[0][1]["cl_ord_id"] == _CID  # the request really went out
        assert not isinstance(info.value, OrderOutcomeUnknownError)

    async def test_timeout_without_cl_ord_id_is_not_outcome_unknown(self) -> None:
        mixin, rest = _mixin("add_order", _ADD_OK)
        rest.raises = httpx.ReadTimeout("response lost")

        with pytest.raises(httpx.ReadTimeout) as info:
            await _place(mixin, userref=77)

        assert not isinstance(info.value, OrderOutcomeUnknownError)

    async def test_codec_encoded_id_is_accepted(self) -> None:
        codec = KrakenClOrdIdLinkCodec()
        tag = codec.encode(KrakenClOrdIdLinkCodec.GROUP_ID_MAX, 15)
        mixin, rest = _mixin("add_order", _ADD_OK)

        await _place(mixin, cl_ord_id=tag)

        assert rest.bodies[0][1]["cl_ord_id"] == tag


# ---------------------------------------------------------------------------
# OrderInfo.cl_ord_id and the lookups
# ---------------------------------------------------------------------------


def _with_cl(txid: str, cl_ord_id: str | None, status: str = "open") -> dict[str, Any]:
    raw = _order_info_raw(txid=txid, status=status)
    if cl_ord_id is not None:
        raw["cl_ord_id"] = cl_ord_id
    return raw


class TestOrderInfoClOrdId:
    def test_defaults_to_none_when_absent(self) -> None:
        assert OrderInfo.model_validate(_order_info_raw()).cl_ord_id is None

    def test_parsed_when_present(self) -> None:
        assert OrderInfo.model_validate(_with_cl("O1", _CID)).cl_ord_id == _CID

    async def test_query_orders_parses_cl_ord_id(self) -> None:
        mixin, _ = _mixin(
            "query_orders",
            {"error": [], "result": {"O1": _with_cl("O1", _CID), "O2": _with_cl("O2", None)}},
        )

        result = await mixin.query_orders(["O1", "O2"])

        assert result["O1"].cl_ord_id == _CID
        assert result["O2"].cl_ord_id is None


class TestLookupsByClOrdId:
    async def test_get_open_orders_by_cl_ord_id(self) -> None:
        reply = {"error": [], "result": {"open": {"O1": _with_cl("O1", _CID)}}}
        mixin, rest = _mixin("open_orders", reply)

        result = await mixin.get_open_orders(cl_ord_id=_CID)

        body = rest.bodies[0][1]
        assert body["cl_ord_id"] == _CID
        assert "userref" not in body
        assert set(result) == {"O1"}
        assert result["O1"].cl_ord_id == _CID

    async def test_get_closed_orders_by_cl_ord_id(self) -> None:
        closed = {"C1": _with_cl("C1", _CID, status="closed")}
        reply = {"error": [], "result": {"closed": closed, "count": 1}}
        mixin, rest = _mixin("closed_orders", reply)

        result = await mixin.get_closed_orders(cl_ord_id=_CID)

        body = rest.bodies[0][1]
        assert body["cl_ord_id"] == _CID
        assert "userref" not in body
        assert set(result) == {"C1"}
        assert result["C1"].cl_ord_id == _CID

    async def test_get_open_orders_both_filters_rejected_before_request(self) -> None:
        mixin, rest = _mixin("open_orders", {"error": [], "result": {"open": {}}})

        with pytest.raises(ValueError):
            await mixin.get_open_orders(5, cl_ord_id=_CID)

        assert rest.bodies == []

    async def test_get_closed_orders_both_filters_rejected_before_request(self) -> None:
        mixin, rest = _mixin("closed_orders", {"error": [], "result": {"closed": {}}})

        with pytest.raises(ValueError):
            await mixin.get_closed_orders(5, cl_ord_id=_CID)

        assert rest.bodies == []


# ---------------------------------------------------------------------------
# cancel_order
# ---------------------------------------------------------------------------


class TestCancelOrderClOrdId:
    async def test_cancel_by_cl_ord_id_sends_no_txid(self) -> None:
        mixin, rest = _mixin("cancel_order", _CANCEL_OK)

        assert await mixin.cancel_order("XBTUSD", cl_ord_id=_CID) is True

        body = rest.bodies[0][1]
        assert body["cl_ord_id"] == _CID
        assert "txid" not in body

    async def test_positional_txid_still_works(self) -> None:
        mixin, rest = _mixin("cancel_order", _CANCEL_OK)

        assert await mixin.cancel_order("XBTUSD", "TXID") is True

        body = rest.bodies[0][1]
        assert body["txid"] == "TXID"
        assert "cl_ord_id" not in body

    async def test_neither_identifier_rejected_before_request(self) -> None:
        mixin, rest = _mixin("cancel_order", _CANCEL_OK)

        with pytest.raises(ValueError):
            await mixin.cancel_order("XBTUSD")

        assert rest.bodies == []

    async def test_both_identifiers_rejected_before_request(self) -> None:
        mixin, rest = _mixin("cancel_order", _CANCEL_OK)

        with pytest.raises(ValueError):
            await mixin.cancel_order("XBTUSD", "TXID", cl_ord_id=_CID)

        assert rest.bodies == []
