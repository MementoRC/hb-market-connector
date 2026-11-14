"""Kraken REST retry hardening (#214 item 6), RED.

The issue (item 6, "_api_request_with_retry rewrite") asks for: (b) Cloudflare/5xx/10xx errors
retried with backoff, and AddOrder never duplicated (re-check by userref first); (c) cancel-only
backoff (done in PR #219, not repeated here); (d) ``EAPI:Invalid nonce`` -> a dedicated error
carrying guidance, NOT retried. The item-4 review adds (e) a grace before an UNKNOWN order is
failed. Contract pinned here (none of it exists yet):

A. Non-idempotent AddOrder. Design: the REST loop NEVER re-sends a non-idempotent endpoint.
   * ``Endpoint.idempotent: bool = True``; ``add_order``, ``add_order_batch`` and ``edit_order``
     are registered with ``idempotent=False``.
   * A 5xx / Cloudflare reply to such a call raises ``ExchangeUnavailableError`` after exactly
     ONE request; a transport error propagates raw after exactly one request.
   * ``KrakenConnectorCore`` treats ``ExchangeUnavailableError`` from AddOrder like a lost
     response: the order goes UNKNOWN and the userref lookup (item 4) decides.
B. Idempotent calls (public, private reads, CancelOrder) retry transient failures:
   * HTTP 5xx (incl. Cloudflare 520-530) AND a non-JSON (HTML) Cloudflare body, whatever the
     status (403 "error code: 10xx", 200 challenge page), and httpx transport errors.
   * Exhaustion raises ``ExchangeUnavailableError`` (also for transport errors).
   * A JSON error body (``{"error": [...]}``) is never treated as transient.
   * Each retry of a private call is RE-SIGNED with a fresh, strictly greater nonce.
C. ``RateLimitError.retry_after: float | None`` (seconds, default ``None``, keyword arg).
   ``EAPI:Rate limit exceeded`` and ``EGeneral:Temporary lockout`` map to ``RateLimitError``
   with a positive hint; the lockout hint is >= 60 s and larger than the rate-limit hint.
D. ``market_connector.exceptions.InvalidNonceError(AuthenticationError)``; maps
   ``EAPI:Invalid nonce``; its message carries guidance (nonce window, API key); the REST layer
   never retries it (one request, AddOrder or read).
E. ``KrakenConnectorCore(unknown_order_grace: float = 0.0)`` (keyword-only, seconds on the
   injected ``clock``): an UNKNOWN order absent from open+closed is failed only once the clock
   is >= ``unknown_order_grace`` past the order's creation; found orders are adopted at once.
   The default 0.0 keeps ``test_userref_recovery_214`` behaviour.

Only the HTTP boundary / REST transport is faked; no real sleeping (retry_delay 0, fake clock).
"""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import httpx
import pytest

from market_connector.exceptions import (
    AuthenticationError,
    ExchangeUnavailableError,
    RateLimitError,
)
from market_connector.exchanges.kraken.endpoints import ENDPOINT_REGISTRY
from market_connector.exchanges.kraken.errors import ERROR_CODE_MAPPING, raise_on_kraken_error
from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway
from market_connector.primitives import OrderType, TradeType
from tests.exchanges.kraken.unit import test_cancel_only_214 as co
from tests.exchanges.kraken.unit import test_kraken_connector_core_211 as base
from tests.exchanges.kraken.unit import test_private_request_wire_215 as wire
from tests.exchanges.kraken.unit import test_userref_recovery_214 as rec

FAILURE, BUY_CREATED = base.FAILURE, base.BUY_CREATED
Recorder = base.Recorder

# Fixtures reused as-is (pytest discovers them by module attribute); ``rest`` is overridden.
bare_connector, bus, connector = base.bare_connector, base.bus, base.connector
gateway, seen = base.gateway, base.seen

NONCE = "EAPI:Invalid nonce"
RATE_LIMIT = "EAPI:Rate limit exceeded"
LOCKOUT = "EGeneral:Temporary lockout"
_EXC_MOD = "market_connector.exceptions"
_BALANCE_OK = {"error": [], "result": {"XXBT": "1.5"}}
_TIME_OK = {"error": [], "result": {"unixtime": 1700000000, "rfc1123": "x"}}
_CF_STATUSES = [520, 521, 522, 523, 524, 525, 526, 527, 530]


# ---------------------------------------------------------------------------
# Scripted HTTP boundary
# ---------------------------------------------------------------------------

Step = Callable[[], httpx.Response | Exception]


def _json(reply: dict[str, Any], status: int = 200) -> Step:
    return lambda: httpx.Response(status, json=reply)


def _html(status: int, body: str = "<html>Cloudflare error code: 1020</html>") -> Step:
    return lambda: httpx.Response(status, text=body, headers={"content-type": "text/html"})


def _boom(exc: Exception) -> Step:
    return lambda: exc


def _scripted(*steps: Step) -> tuple[KrakenGateway, list[httpx.Request]]:
    """A started gateway whose HTTP answers follow ``steps`` (the last one repeats)."""
    gw = KrakenGateway(wire._API_KEY, wire._SECRET)  # noqa: SLF001
    gw._started = True
    gw._rest._retry_delay = 0.0  # type: ignore[attr-defined]  # no real sleeping
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        outcome = steps[min(len(sent), len(steps)) - 1]()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    gw._rest._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[attr-defined]
    return gw, sent


async def _place(gw: KrakenGateway) -> str:
    return await gw.place_order(
        "XBTUSD", OrderType.LIMIT, TradeType.BUY, Decimal("0.01"), Decimal("100.5"), userref=7
    )


def _exc(name: str) -> Any:
    return getattr(importlib.import_module(_EXC_MOD), name)


# ---------------------------------------------------------------------------
# A. Non-idempotent AddOrder is never re-sent by the REST loop
# ---------------------------------------------------------------------------


class TestAIdempotencyFlag:
    def test_endpoint_has_an_idempotent_flag_defaulting_to_true(self) -> None:
        from market_connector.transport.endpoint import Endpoint

        assert Endpoint(path="/x", method="GET").idempotent is True  # type: ignore[attr-defined]

    @pytest.mark.parametrize("name", ["add_order", "add_order_batch", "edit_order"])
    def test_order_creating_endpoints_are_not_idempotent(self, name: str) -> None:
        assert ENDPOINT_REGISTRY[name].idempotent is False  # type: ignore[attr-defined]

    @pytest.mark.parametrize(
        "name",
        ["balance", "balance_ex", "open_orders", "closed_orders", "query_orders", "cancel_order"],
    )
    def test_reads_and_cancels_stay_idempotent(self, name: str) -> None:
        assert ENDPOINT_REGISTRY[name].idempotent is True  # type: ignore[attr-defined]


class TestANoBlindResend:
    @pytest.mark.parametrize("status", [500, 503, *_CF_STATUSES])
    async def test_add_order_5xx_is_sent_exactly_once(self, status: int) -> None:
        gw, sent = _scripted(
            _html(status, "<html>boom</html>"), _json(wire._KRAKEN_ADD_ORDER_REPLY)
        )  # noqa: SLF001

        with pytest.raises(ExchangeUnavailableError):
            await _place(gw)

        assert len(sent) == 1, f"AddOrder re-sent {len(sent)} times"

    async def test_add_order_cloudflare_block_page_is_sent_exactly_once(self) -> None:
        gw, sent = _scripted(_html(403), _json(wire._KRAKEN_ADD_ORDER_REPLY))  # noqa: SLF001

        with pytest.raises(ExchangeUnavailableError):
            await _place(gw)

        assert len(sent) == 1

    @pytest.mark.parametrize(
        "exc",
        [httpx.ReadTimeout("lost"), httpx.ConnectError("reset")],
        ids=["timeout", "transport"],
    )
    async def test_add_order_transport_error_propagates_raw_after_one_request(
        self, exc: Exception
    ) -> None:
        gw, sent = _scripted(_boom(exc), _json(wire._KRAKEN_ADD_ORDER_REPLY))  # noqa: SLF001

        with pytest.raises(type(exc)):
            await _place(gw)

        assert len(sent) == 1

    async def test_cancel_order_5xx_is_still_retried(self) -> None:
        gw, sent = _scripted(_html(503, "x"), _json({"error": [], "result": {"count": 1}}))

        assert await gw.cancel_order("XBTUSD", "OTXID-1") is True

        assert len(sent) == 2


@pytest.fixture
def rest() -> rec.UserrefRest:
    return rec.UserrefRest()


class TestACoreTreatsUnavailableAddOrderAsUnknown:
    async def test_5xx_on_add_order_leaves_the_order_unknown_not_failed(
        self, connector: Any, rest: rec.UserrefRest, seen: Recorder
    ) -> None:
        exc = ExchangeUnavailableError("520 from add_order: cloudflare")
        cid = await rec._lose_one(connector, rest, exc, lost_as=None)  # noqa: SLF001

        assert seen.of(FAILURE) == []
        assert cid in connector.in_flight_orders
        assert rec._state_name(connector, cid) == "UNKNOWN"  # noqa: SLF001

    async def test_order_accepted_despite_the_5xx_is_adopted_by_userref(
        self, connector: Any, rest: rec.UserrefRest, seen: Recorder
    ) -> None:
        exc = ExchangeUnavailableError("522 from add_order")
        cid = await rec._lose_one(connector, rest, exc, lost_as="open")  # noqa: SLF001
        txid = f"OLOST-{rest.userrefs[0]}"

        await connector._resolve_unknown_orders()  # noqa: SLF001

        assert connector.fetch_order(cid).exchange_order_id == txid
        assert [e.exchange_order_id for e in seen.of(BUY_CREATED)] == [txid]
        assert seen.of(FAILURE) == []
        assert len(rest.bodies("add_order")) == 1  # never re-sent

    async def test_rate_limit_rejection_of_add_order_is_definite(
        self, connector: Any, rest: rec.UserrefRest, seen: Recorder
    ) -> None:
        rest.add_order_errors = [LOCKOUT]

        cid = rec._buy(connector)  # noqa: SLF001
        await seen.wait(FAILURE)

        assert [f.order_id for f in seen.of(FAILURE)] == [cid]
        assert rest.lookups() == []


# ---------------------------------------------------------------------------
# B. Idempotent calls retry transient failures (Cloudflare, 10xx, transport)
# ---------------------------------------------------------------------------


class TestBTransientRetries:
    @pytest.mark.parametrize("status", [500, 502, 503, 504, *_CF_STATUSES])
    async def test_private_read_retries_5xx_and_cloudflare_statuses(self, status: int) -> None:
        gw, sent = _scripted(_html(status, "<html>edge error</html>"), _json(_BALANCE_OK))

        assert await gw.get_balances() == {"XXBT": Decimal("1.5")}

        assert len(sent) == 2

    @pytest.mark.parametrize("status", [403, 200, 503])
    @pytest.mark.parametrize("code", [1000, 1015, 1020])
    async def test_cloudflare_10xx_html_body_is_transient_for_public_calls(
        self, status: int, code: int
    ) -> None:
        page = f"<html><title>Cloudflare</title>error code: {code}</html>"
        gw, sent = _scripted(_html(status, page), _json(_TIME_OK))

        assert await gw.get_server_time() == 1700000000

        assert len(sent) == 2

    async def test_cloudflare_10xx_html_body_is_transient_for_private_reads(self) -> None:
        gw, sent = _scripted(_html(403), _json(_BALANCE_OK))

        assert await gw.get_balances() == {"XXBT": Decimal("1.5")}

        assert len(sent) == 2

    async def test_persistent_cloudflare_page_exhausts_into_exchange_unavailable(self) -> None:
        gw, sent = _scripted(_html(403))
        max_retries = gw._rest._max_retries  # type: ignore[attr-defined]

        with pytest.raises(ExchangeUnavailableError):
            await gw.get_balances()

        assert len(sent) == max_retries + 1

    async def test_html_error_page_never_crashes_on_json_decoding(self) -> None:
        gw, _ = _scripted(_html(520, "<!DOCTYPE html><html>not json</html>"))

        with pytest.raises(ExchangeUnavailableError):
            await gw.get_server_time()

    @pytest.mark.parametrize(
        "exc",
        [httpx.ReadTimeout("slow"), httpx.ConnectError("reset"), httpx.RemoteProtocolError("eof")],
        ids=["timeout", "connect", "protocol"],
    )
    async def test_transport_errors_are_retried_for_idempotent_calls(self, exc: Exception) -> None:
        gw, sent = _scripted(_boom(exc), _json(_BALANCE_OK))

        assert await gw.get_balances() == {"XXBT": Decimal("1.5")}

        assert len(sent) == 2

    async def test_persistent_transport_error_exhausts_into_exchange_unavailable(self) -> None:
        gw, sent = _scripted(_boom(httpx.ConnectError("down")))
        max_retries = gw._rest._max_retries  # type: ignore[attr-defined]

        with pytest.raises(ExchangeUnavailableError):
            await gw.get_balances()

        assert len(sent) == max_retries + 1

    async def test_json_error_body_is_not_transient(self) -> None:
        gw, sent = _scripted(_json({"error": ["EGeneral:Permission denied"]}, status=403))

        with pytest.raises(Exception, match="Permission denied"):
            await gw.get_balances()

        assert len(sent) == 1

    async def test_kraken_error_in_a_200_reply_is_not_retried(self) -> None:
        gw, sent = _scripted(_json({"error": ["EService:Busy"]}))

        with pytest.raises(Exception, match="Busy"):
            await gw.get_balances()

        assert len(sent) == 1


class TestBRetriesUseAFreshNonce:
    async def test_retried_private_call_is_resigned_with_a_greater_nonce(self) -> None:
        gw, sent = _scripted(_html(520, "x"), _html(520, "x"), _json(_BALANCE_OK))

        await gw.get_balances()

        assert len(sent) == 3
        nonces = [int(wire._sent_nonce(r)) for r in sent]  # noqa: SLF001
        assert all(a < b for a, b in zip(nonces, nonces[1:], strict=False)), nonces

    async def test_each_retry_carries_a_valid_signature_over_its_own_body(self) -> None:
        gw, sent = _scripted(_html(503, "x"), _json(_BALANCE_OK))

        await gw.get_balances()

        assert len(sent) == 2
        for request in sent:
            assert request.headers["API-Sign"] == wire._kraken_expected_sign(request)  # noqa: SLF001
        assert sent[0].content != sent[1].content, "retry re-sent the identical (stale-nonce) body"


# ---------------------------------------------------------------------------
# C. Rate limit and lockout map to RateLimitError with a retry-after hint
# ---------------------------------------------------------------------------


def _hint(code: str) -> Any:
    with pytest.raises(RateLimitError) as info:
        raise_on_kraken_error([code])
    return info.value.retry_after  # type: ignore[attr-defined]


class TestCRateLimitGuidance:
    @pytest.mark.parametrize("code", [RATE_LIMIT, LOCKOUT])
    def test_codes_map_to_rate_limit_error(self, code: str) -> None:
        assert ERROR_CODE_MAPPING[code] is RateLimitError

    def test_rate_limit_error_carries_an_optional_retry_after(self) -> None:
        assert RateLimitError("429").retry_after is None  # type: ignore[attr-defined]
        assert RateLimitError("x", retry_after=5.0).retry_after == 5.0  # type: ignore[call-arg,attr-defined]

    def test_retry_after_is_keyword_only(self) -> None:
        param = inspect.signature(RateLimitError.__init__).parameters["retry_after"]

        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default is None

    @pytest.mark.parametrize("code", [RATE_LIMIT, LOCKOUT])
    def test_kraken_codes_expose_a_positive_float_hint(self, code: str) -> None:
        hint = _hint(code)

        assert isinstance(hint, float)
        assert hint > 0

    def test_lockout_hint_is_at_least_a_minute_and_longer_than_a_plain_rate_limit(self) -> None:
        assert _hint(LOCKOUT) >= 60.0
        assert _hint(LOCKOUT) > _hint(RATE_LIMIT)

    async def test_hint_survives_the_gateway_path(self) -> None:
        gw, sent = _scripted(_json({"error": [LOCKOUT]}))

        with pytest.raises(RateLimitError) as info:
            await gw.get_balances()

        assert info.value.retry_after >= 60.0  # type: ignore[attr-defined]
        assert len(sent) == 1  # a lockout is never hammered


# ---------------------------------------------------------------------------
# D. Invalid nonce: dedicated error with guidance, never retried
# ---------------------------------------------------------------------------


class TestDInvalidNonce:
    def test_invalid_nonce_error_is_an_authentication_error(self) -> None:
        assert issubclass(_exc("InvalidNonceError"), AuthenticationError)

    def test_error_code_maps_to_the_dedicated_class(self) -> None:
        assert ERROR_CODE_MAPPING[NONCE] is _exc("InvalidNonceError")

    def test_message_carries_nonce_window_and_api_key_guidance(self) -> None:
        with pytest.raises(_exc("InvalidNonceError")) as info:
            raise_on_kraken_error([NONCE])

        message = str(info.value).lower()
        assert NONCE.lower() in message
        assert "nonce window" in message
        assert "api key" in message

    async def test_private_read_is_not_retried_on_invalid_nonce(self) -> None:
        gw, sent = _scripted(_json({"error": [NONCE]}), _json(_BALANCE_OK))

        with pytest.raises(_exc("InvalidNonceError")):
            await gw.get_balances()

        assert len(sent) == 1

    async def test_add_order_is_never_retried_on_invalid_nonce(self) -> None:
        gw, sent = _scripted(_json({"error": [NONCE]}), _json(wire._KRAKEN_ADD_ORDER_REPLY))  # noqa: SLF001

        with pytest.raises(_exc("InvalidNonceError")):
            await _place(gw)

        assert len(sent) == 1

    async def test_core_fails_the_order_definitely_on_invalid_nonce(
        self, connector: Any, rest: rec.UserrefRest, seen: Recorder
    ) -> None:
        rest.add_order_errors = [NONCE]

        cid = rec._buy(connector)  # noqa: SLF001
        await seen.wait(FAILURE)

        assert [f.order_id for f in seen.of(FAILURE)] == [cid]
        assert connector.fetch_order(cid).current_state.name == "FAILED"


# ---------------------------------------------------------------------------
# E. unknown_order_grace: do not fail an UNKNOWN order on its first miss
# ---------------------------------------------------------------------------

_GRACE = 30.0


@pytest.fixture
def clock() -> co.FakeClock:
    return co.FakeClock()


@pytest.fixture
async def graced(gateway: Any, bus: Any, seen: Recorder, clock: co.FakeClock) -> Any:
    cls = importlib.import_module(base._CORE_MOD).KrakenConnectorCore  # noqa: SLF001
    core = cls(gateway, event_bus=bus, clock=clock, unknown_order_grace=_GRACE)
    await core.update_trading_rules()
    return core


class TestEUnknownOrderGrace:
    def test_knob_is_keyword_only_and_defaults_to_zero(self) -> None:
        cls = importlib.import_module(base._CORE_MOD).KrakenConnectorCore  # noqa: SLF001
        param = inspect.signature(cls).parameters["unknown_order_grace"]

        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default == 0.0

    async def test_miss_inside_the_grace_keeps_the_order_unknown(
        self, graced: Any, rest: rec.UserrefRest, seen: Recorder, clock: co.FakeClock
    ) -> None:
        cid = await rec._lose_one(graced, rest, rec._LOST[0], lost_as=None)  # noqa: SLF001
        clock.now += _GRACE - 1

        await graced._resolve_unknown_orders()  # noqa: SLF001
        await graced._resolve_unknown_orders()  # noqa: SLF001

        assert seen.of(FAILURE) == []
        assert cid in graced.in_flight_orders
        assert rec._state_name(graced, cid) == "UNKNOWN"  # noqa: SLF001
        assert [n for n, _ in rest.lookups()][:2] == ["open_orders", "closed_orders"]

    async def test_miss_after_the_grace_fails_exactly_once(
        self, graced: Any, rest: rec.UserrefRest, seen: Recorder, clock: co.FakeClock
    ) -> None:
        cid = await rec._lose_one(graced, rest, rec._LOST[1], lost_as=None)  # noqa: SLF001
        clock.now += _GRACE + 1

        await graced._resolve_unknown_orders()  # noqa: SLF001
        await graced._resolve_unknown_orders()  # noqa: SLF001

        assert [f.order_id for f in seen.of(FAILURE)] == [cid]
        assert cid not in graced.in_flight_orders

    async def test_order_that_appears_during_the_grace_is_adopted_later(
        self, graced: Any, rest: rec.UserrefRest, seen: Recorder, clock: co.FakeClock
    ) -> None:
        cid = await rec._lose_one(graced, rest, rec._LOST[0], lost_as=None)  # noqa: SLF001
        await graced._resolve_unknown_orders()  # noqa: SLF001  # first miss: still tracked
        txid = rest.register_on_exchange(rest.userrefs[0], "open")  # propagation delay ends
        clock.now += _GRACE / 2

        await graced._resolve_unknown_orders()  # noqa: SLF001

        assert graced.fetch_order(cid).exchange_order_id == txid
        assert [e.exchange_order_id for e in seen.of(BUY_CREATED)] == [txid]
        assert seen.of(FAILURE) == []

    async def test_found_order_is_adopted_at_once_inside_the_grace(
        self, graced: Any, rest: rec.UserrefRest, seen: Recorder
    ) -> None:
        cid = await rec._lose_one(graced, rest, rec._LOST[0], lost_as="closed")  # noqa: SLF001

        await graced._resolve_unknown_orders()  # noqa: SLF001

        assert graced.fetch_order(cid).exchange_order_id == f"OLOST-{rest.userrefs[0]}"
        assert seen.of(FAILURE) == []

    async def test_poller_honours_the_grace(
        self, graced: Any, rest: rec.UserrefRest, seen: Recorder, clock: co.FakeClock
    ) -> None:
        cid = await rec._lose_one(graced, rest, rec._LOST[0], lost_as=None)  # noqa: SLF001

        await graced._poll_once()  # noqa: SLF001
        assert seen.of(FAILURE) == []
        assert cid in graced.in_flight_orders

        clock.now += _GRACE + 1
        await graced._poll_once()  # noqa: SLF001

        assert [f.order_id for f in seen.of(FAILURE)] == [cid]
