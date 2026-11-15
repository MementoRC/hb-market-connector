"""Kraken place_order: dtp.common members, strings, local enums give identical payloads (#231)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from data_type_primitives.common import OrderType as DtpOrderType
from data_type_primitives.common import TradeType as DtpTradeType

from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway
from market_connector.primitives import OrderType, TradeType
from market_connector.transport.response import Response

_ADD_ORDER_OK = {"error": [], "result": {"descr": {"order": "x"}, "txid": ["OABC-XXXXX-YYYYYY"]}}
_PAIR = "XBTUSD"
_AMOUNT = Decimal("0.5")

# canonical member name -> (price, limit_price) needed by that type
_KWARGS: dict[str, tuple[Decimal | None, Decimal | None]] = {
    "MARKET": (None, None),
    "LIMIT": (Decimal("50000"), None),
    "LIMIT_MAKER": (Decimal("50000"), None),
    "STOP_LOSS": (Decimal("49000"), None),
    "TAKE_PROFIT": (Decimal("51000"), None),
    "STOP_LOSS_LIMIT": (Decimal("49000"), Decimal("48900")),
    "TAKE_PROFIT_LIMIT": (Decimal("51000"), Decimal("51100")),
    "TRAILING_STOP": (Decimal("150.5"), None),
    "TRAILING_STOP_LIMIT": (Decimal("150.5"), Decimal("-5")),
}
_NAMES = list(_KWARGS)
_LOCAL_NAMES = [n for n in _NAMES if n in OrderType.__members__]


class _RecordingRest:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def request(
        self, endpoint_name: str, data: dict[str, Any] | None = None, **_: Any
    ) -> Response:
        self.calls.append((endpoint_name, dict(data or {})))
        return Response(
            raw=_ADD_ORDER_OK, status_code=200, headers=httpx.Headers(), _endpoint=endpoint_name
        )

    @property
    def bodies(self) -> list[dict[str, Any]]:
        return [b for n, b in self.calls if n == "add_order"]


@pytest.fixture
def rest() -> _RecordingRest:
    return _RecordingRest()


@pytest.fixture
def gateway(rest: _RecordingRest) -> KrakenGateway:
    gw = KrakenGateway(api_key="k", secret_key="c2VjcmV0")
    gw._rest = rest  # type: ignore[assignment]
    gw._started = True
    return gw


async def _body(
    gateway: KrakenGateway,
    rest: _RecordingRest,
    name: str,
    order_type: Any,
    side: Any = TradeType.BUY,
) -> dict[str, Any]:
    rest.calls.clear()
    price, limit_price = _KWARGS[name]
    await gateway.place_order(_PAIR, order_type, side, _AMOUNT, price, limit_price=limit_price)
    (body,) = rest.bodies
    return body


@pytest.mark.parametrize("name", _NAMES)
async def test_dtp_member_payload_matches_string(
    gateway: KrakenGateway, rest: _RecordingRest, name: str
) -> None:
    from_str = await _body(gateway, rest, name, name.lower())
    assert await _body(gateway, rest, name, DtpOrderType[name]) == from_str


@pytest.mark.parametrize("name", _NAMES)
@pytest.mark.parametrize("case", [str.lower, str.upper, str.title])
async def test_string_case_insensitive_payload_identical(
    gateway: KrakenGateway, rest: _RecordingRest, name: str, case: Any
) -> None:
    baseline = await _body(gateway, rest, name, DtpOrderType[name])
    assert await _body(gateway, rest, name, case(name)) == baseline


@pytest.mark.parametrize("name", _LOCAL_NAMES)
async def test_local_enum_payload_matches_dtp(
    gateway: KrakenGateway, rest: _RecordingRest, name: str
) -> None:
    baseline = await _body(gateway, rest, name, DtpOrderType[name])
    assert await _body(gateway, rest, name, OrderType[name]) == baseline


@pytest.mark.parametrize(
    ("side", "wire"),
    [
        pytest.param(DtpTradeType.BUY, "buy", id="dtp-buy"),
        pytest.param(DtpTradeType.SELL, "sell", id="dtp-sell"),
        pytest.param(TradeType.SELL, "sell", id="local-sell"),
        pytest.param("BUY", "buy", id="str-upper"),
        pytest.param("sell", "sell", id="str-lower"),
    ],
)
async def test_side_normalised(
    gateway: KrakenGateway, rest: _RecordingRest, side: Any, wire: str
) -> None:
    body = await _body(gateway, rest, "LIMIT", DtpOrderType.LIMIT, side)
    assert body["type"] == wire


@pytest.mark.parametrize(
    ("name", "wire"),
    [
        ("TAKE_PROFIT", "take-profit"),
        ("TAKE_PROFIT_LIMIT", "take-profit-limit"),
        ("TRAILING_STOP", "trailing-stop"),
        ("TRAILING_STOP_LIMIT", "trailing-stop-limit"),
    ],
)
async def test_dtp_only_types_wire_ordertype(
    gateway: KrakenGateway, rest: _RecordingRest, name: str, wire: str
) -> None:
    body = await _body(gateway, rest, name, DtpOrderType[name])
    assert body["ordertype"] == wire


async def test_dtp_limit_maker_is_post_only(gateway: KrakenGateway, rest: _RecordingRest) -> None:
    body = await _body(gateway, rest, "LIMIT_MAKER", DtpOrderType.LIMIT_MAKER)
    assert body["ordertype"] == "limit"
    assert body["oflags"] == "post"


@pytest.mark.parametrize("bad", [DtpOrderType.AMM_SWAP, "bogus"])
async def test_unsupported_raises_before_http(
    gateway: KrakenGateway, rest: _RecordingRest, bad: Any
) -> None:
    with pytest.raises(ValueError, match="Unsupported order type"):
        await gateway.place_order(_PAIR, bad, TradeType.BUY, _AMOUNT, Decimal("1"))
    assert rest.calls == []
