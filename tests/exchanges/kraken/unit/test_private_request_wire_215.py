"""Wire-level regression tests for #215: private REST requests as actually sent.

Interception is at the httpx boundary (``httpx.MockTransport``), so every assertion
is made on the real ``httpx.Request`` the transport built: method, url, headers and
content bytes. Kraken tests encode Kraken's documented private-call contract:

* form-urlencoded body (``application/x-www-form-urlencoded``) containing ``nonce``
* ``API-Sign = base64(HMAC-SHA512(b64decode(secret), uri_path + SHA256(nonce + body)))``
  computed over the exact bytes sent
* a time-based, strictly increasing nonce that survives a process restart
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import time
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import httpx

from market_connector.exchanges.coinbase.coinbase_gateway import CoinbaseGateway
from market_connector.exchanges.coinbase.config import CoinbaseConfig
from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway
from market_connector.primitives import OrderType, TradeType

_API_KEY = "test-api-key"
_SECRET = base64.b64encode(b"fixed-test-secret-0123456789abcdef").decode()

_KRAKEN_BALANCE_REPLY = {"error": [], "result": {"XXBT": "1.5"}}
_KRAKEN_ADD_ORDER_REPLY = {
    "error": [],
    "result": {"txid": ["OABCDE-12345-FGHIJK"], "descr": {"order": "test"}},
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _install_capture(rest: object, reply: dict, sent: list[httpx.Request]) -> None:
    """Replace the transport's httpx client with one backed by a recording MockTransport."""

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=reply)

    rest._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[attr-defined]


def _kraken(reply: dict) -> tuple[KrakenGateway, list[httpx.Request]]:
    gateway = KrakenGateway(_API_KEY, _SECRET)
    gateway._started = True  # skip start(): no server_time / WebSocket
    sent: list[httpx.Request] = []
    _install_capture(gateway._rest, reply, sent)
    return gateway, sent


def _body_text(request: httpx.Request) -> str:
    return request.content.decode("utf-8")


def _assert_form_encoded(request: httpx.Request) -> dict[str, list[str]]:
    """Assert the request is a form-urlencoded POST carrying a nonce; return its fields."""
    content_type = request.headers.get("content-type", "")
    assert content_type.startswith("application/x-www-form-urlencoded"), (
        f"Content-Type is {content_type!r}, body is {request.content!r}"
    )
    fields = parse_qs(_body_text(request), keep_blank_values=True)
    assert "nonce" in fields, f"no nonce in sent body {request.content!r}"
    assert fields["nonce"][0].isdigit()
    return fields


def _sent_nonce(request: httpx.Request) -> str:
    return _assert_form_encoded(request)["nonce"][0]


def _kraken_expected_sign(request: httpx.Request) -> str:
    """Kraken's documented API-Sign over the exact bytes sent."""
    body = _body_text(request)
    nonce = _sent_nonce(request)
    digest = hashlib.sha256((nonce + body).encode()).digest()
    mac = hmac.new(
        base64.b64decode(_SECRET),
        request.url.path.encode() + digest,
        hashlib.sha512,
    )
    return base64.b64encode(mac.digest()).decode()


# ---------------------------------------------------------------------------
# 1. Balance
# ---------------------------------------------------------------------------


async def test_balance_sends_form_urlencoded_body_with_nonce() -> None:
    gateway, sent = _kraken(_KRAKEN_BALANCE_REPLY)

    await gateway.get_balances()

    assert len(sent) == 1
    request = sent[0]
    assert request.method == "POST"
    assert request.url.path == "/0/private/Balance"
    fields = _assert_form_encoded(request)
    assert set(fields) == {"nonce"}
    assert not request.content.lstrip().startswith(b"{"), "JSON body sent"


# ---------------------------------------------------------------------------
# 2. AddOrder LIMIT
# ---------------------------------------------------------------------------


async def test_add_order_limit_body_is_form_urlencoded_with_params_and_nonce() -> None:
    gateway, sent = _kraken(_KRAKEN_ADD_ORDER_REPLY)

    await gateway.place_order(
        "XBTUSD", OrderType.LIMIT, TradeType.BUY, Decimal("0.01"), Decimal("100.5")
    )

    request = sent[0]
    assert request.url.path == "/0/private/AddOrder"
    fields = _assert_form_encoded(request)
    assert {k: v[0] for k, v in fields.items() if k != "nonce"} == {
        "pair": "XBTUSD",
        "type": "buy",
        "ordertype": "limit",
        "volume": "0.01",
        "price": "100.5",
    }


# ---------------------------------------------------------------------------
# 3. Percent-encoding of '#', '%' and '+'
# ---------------------------------------------------------------------------


async def test_add_order_stop_loss_percent_and_plus_are_percent_encoded() -> None:
    gateway, sent = _kraken(_KRAKEN_ADD_ORDER_REPLY)

    await gateway.place_order(
        "XBTUSD",
        OrderType.STOP_LOSS_LIMIT,
        TradeType.SELL,
        Decimal("0.01"),
        Decimal("5"),
        limit_price="+1",  # type: ignore[arg-type]  # str() is applied; Decimal cannot carry '+'
        price_in_percent=True,
    )

    request = sent[0]
    _assert_form_encoded(request)
    raw = _body_text(request)
    assert "price=%235%25" in raw.split("&"), raw
    assert "price2=%2B1" in raw.split("&"), raw
    fields = parse_qs(raw)
    assert fields["price"] == ["#5%"]
    assert fields["price2"] == ["+1"]


# ---------------------------------------------------------------------------
# 4. API-Sign over the exact sent bytes
# ---------------------------------------------------------------------------


async def test_balance_api_sign_matches_documented_algorithm_over_sent_bytes() -> None:
    gateway, sent = _kraken(_KRAKEN_BALANCE_REPLY)

    await gateway.get_balances()

    request = sent[0]
    assert request.headers["API-Key"] == _API_KEY
    assert request.headers["API-Sign"] == _kraken_expected_sign(request)


async def test_limit_order_api_sign_matches_documented_algorithm_over_sent_bytes() -> None:
    gateway, sent = _kraken(_KRAKEN_ADD_ORDER_REPLY)

    await gateway.place_order(
        "XBTUSD", OrderType.LIMIT, TradeType.BUY, Decimal("0.01"), Decimal("100.5")
    )

    request = sent[0]
    assert request.headers["API-Sign"] == _kraken_expected_sign(request)


async def test_stop_loss_order_api_sign_matches_documented_algorithm_over_sent_bytes() -> None:
    gateway, sent = _kraken(_KRAKEN_ADD_ORDER_REPLY)

    await gateway.place_order(
        "XBTUSD",
        OrderType.STOP_LOSS,
        TradeType.SELL,
        Decimal("0.01"),
        Decimal("5"),
        price_in_percent=True,
    )

    request = sent[0]
    assert request.headers["API-Sign"] == _kraken_expected_sign(request)


# ---------------------------------------------------------------------------
# 5. Nonce: time-based and strictly monotonic
# ---------------------------------------------------------------------------


async def test_nonce_is_unique_increasing_and_survives_new_gateway_instance() -> None:
    start_ms = int(time.time() * 1000)
    gateway, sent = _kraken(_KRAKEN_BALANCE_REPLY)

    await asyncio.gather(*(gateway.get_balances() for _ in range(5)))

    nonces = [int(_sent_nonce(request)) for request in sent]
    assert len(nonces) == 5
    ordered = sorted(nonces)
    assert all(a < b for a, b in zip(ordered, ordered[1:], strict=False)), (
        f"duplicate nonces: {nonces}"
    )
    assert min(nonces) >= start_ms, f"nonce below current time in ms: {nonces}"

    fresh_gateway, fresh_sent = _kraken(_KRAKEN_BALANCE_REPLY)
    await fresh_gateway.get_balances()

    assert int(_sent_nonce(fresh_sent[0])) > max(nonces)


# ---------------------------------------------------------------------------
# 7. Coinbase (shares transport/rest_base.py and DeclarativeRestSigner)
# ---------------------------------------------------------------------------


async def test_coinbase_hmac_order_is_signed_over_the_body_actually_sent() -> None:
    """Coinbase legacy HMAC recipe (spec): hex(HMAC-SHA256(secret, ts + METHOD + path + body)).

    Coinbase documents the signed requestPath as the full path of the URL sent
    (including the /api/v3 prefix).
    """
    secret = "coinbase-hmac-secret"  # not a PEM, so the HMAC signer is selected
    gateway = CoinbaseGateway(CoinbaseConfig(api_key=_API_KEY, secret_key=secret))
    gateway._started = True
    sent: list[httpx.Request] = []
    _install_capture(gateway._rest, {"success": True, "order_id": "abc"}, sent)

    await gateway.place_order("BTC-USD", OrderType.LIMIT, "BUY", Decimal("0.01"), Decimal("100.5"))

    request = sent[0]
    assert request.method == "POST"
    assert request.content, "no body sent"
    timestamp = request.headers["CB-ACCESS-TIMESTAMP"]
    body = _body_text(request)
    path = urlparse(str(request.url)).path
    assert path == "/api/v3/brokerage/orders"
    expected = hmac.new(
        secret.encode(),
        f"{timestamp}{request.method}{path}{body}".encode(),
        hashlib.sha256,
    ).hexdigest()
    assert request.headers["CB-ACCESS-SIGN"] == expected
