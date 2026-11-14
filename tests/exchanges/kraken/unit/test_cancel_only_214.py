"""Kraken cancel-only / post-only trading state (#214 item 5), RED.

Contract pinned here (none of it exists yet):

A. Mapping (``market_connector.exceptions`` + ``kraken/errors.py``)
   * ``ExchangeCancelOnlyError(ServiceUnavailableError)`` is added next to
     ``ServiceUnavailableError``; ``EService:Market in cancel_only mode`` maps to it.
   * ``ExchangePostOnlyError(ServiceUnavailableError)`` (a separate class, NOT a subclass
     of ``ExchangeCancelOnlyError``) maps ``EService:Market in post_only mode``.
B. CancelOrder is never blocked by cancel-only handling.
C. Backoff in ``KrakenConnectorCore``: constructor kwargs ``clock`` (``() -> float``,
   default ``time.monotonic``), ``cancel_only_backoff`` (initial window, seconds) and
   ``cancel_only_backoff_max`` (cap). A cancel-only reply opens a window; inside it a
   new order fails fast with NO HTTP call; the window doubles on each further
   cancel-only reply and resets after a successful AddOrder. Post-only and plain
   ``EService:Unavailable`` never open a window.
D. A cancel-only rejection is definite: ``MarketOrderFailureEvent``, never UNKNOWN.
E. Plain ``EService:Unavailable`` still maps to ``ServiceUnavailableError`` exactly.

Only the REST transport is faked; the clock is injected (no sleeping).
"""

from __future__ import annotations

import importlib
from decimal import Decimal
from typing import Any

import pytest

from market_connector.exceptions import ServiceUnavailableError
from market_connector.exchanges.kraken.errors import ERROR_CODE_MAPPING
from market_connector.primitives import OrderType, TradeType
from tests.exchanges.kraken.unit import test_kraken_connector_core_211 as base
from tests.exchanges.kraken.unit import test_private_request_wire_215 as wire

BUY_CREATED, FAILURE, CANCELLED = base.BUY_CREATED, base.FAILURE, base.CANCELLED
_AMOUNT, _PAIR, _PRICE = base._AMOUNT, base._PAIR, base._PRICE  # noqa: SLF001
Recorder, FakeKrakenRest = base.Recorder, base.FakeKrakenRest

# Fixtures reused as-is (pytest discovers them by module attribute).
bus, gateway, rest, seen = base.bus, base.gateway, base.rest, base.seen

CANCEL_ONLY = "EService:Market in cancel_only mode"
POST_ONLY = "EService:Market in post_only mode"
UNAVAILABLE = "EService:Unavailable"

_BACKOFF, _BACKOFF_MAX = 10.0, 40.0
_EXC_MOD = "market_connector.exceptions"


def _cancel_only_cls() -> type[Exception]:
    return getattr(importlib.import_module(_EXC_MOD), "ExchangeCancelOnlyError")  # noqa: B009


def _post_only_cls() -> type[Exception]:
    return getattr(importlib.import_module(_EXC_MOD), "ExchangePostOnlyError")  # noqa: B009


async def _place_via_gateway(error: str) -> None:
    gw, _ = wire._kraken({"error": [error]})  # noqa: SLF001
    await gw.place_order("XBTUSD", OrderType.LIMIT, TradeType.BUY, Decimal("0.01"), Decimal("100"))


# ---------------------------------------------------------------------------
# A. Mapping
# ---------------------------------------------------------------------------


class TestAMapping:
    def test_cancel_only_error_is_a_service_unavailable_error(self) -> None:
        assert issubclass(_cancel_only_cls(), ServiceUnavailableError)

    def test_post_only_error_is_a_service_unavailable_error_but_not_cancel_only(self) -> None:
        assert issubclass(_post_only_cls(), ServiceUnavailableError)
        assert not issubclass(_post_only_cls(), _cancel_only_cls())

    def test_error_code_mapping_points_at_the_dedicated_classes(self) -> None:
        assert ERROR_CODE_MAPPING[CANCEL_ONLY] is _cancel_only_cls()
        assert ERROR_CODE_MAPPING[POST_ONLY] is _post_only_cls()

    async def test_add_order_in_cancel_only_mode_raises_cancel_only_error(self) -> None:
        with pytest.raises(_cancel_only_cls()) as info:
            await _place_via_gateway(CANCEL_ONLY)

        assert isinstance(info.value, ServiceUnavailableError)

    async def test_add_order_in_post_only_mode_raises_post_only_error(self) -> None:
        with pytest.raises(_post_only_cls()) as info:
            await _place_via_gateway(POST_ONLY)

        assert not isinstance(info.value, _cancel_only_cls())


# ---------------------------------------------------------------------------
# B. CancelOrder is not blocked
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def core(gateway: Any, bus: Any, seen: Recorder, clock: FakeClock) -> Any:
    cls = importlib.import_module(base._CORE_MOD).KrakenConnectorCore  # noqa: SLF001
    connector = cls(
        gateway,
        event_bus=bus,
        clock=clock,
        cancel_only_backoff=_BACKOFF,
        cancel_only_backoff_max=_BACKOFF_MAX,
    )
    await connector.update_trading_rules()
    return connector


def _buy(core: Any) -> str:
    return core.buy(_PAIR, _AMOUNT, base.OrderType.LIMIT, _PRICE)


async def _rejected(core: Any, rest: FakeKrakenRest, seen: Recorder, error: str) -> str:
    """Submit one buy that Kraken answers with ``error``; wait for its failure event."""
    rest.add_order_errors = [error]
    expected = len(seen.of(FAILURE)) + 1
    cid = _buy(core)
    await seen.wait(FAILURE, expected)
    return cid


async def _accepted(core: Any, rest: FakeKrakenRest, seen: Recorder) -> str:
    rest.add_order_errors = []
    expected = len(seen.of(BUY_CREATED)) + 1
    cid = _buy(core)
    await seen.wait(BUY_CREATED, expected)
    return cid


class TestBCancelStillAllowed:
    async def test_gateway_cancel_order_has_no_cancel_only_special_case(self) -> None:
        gw, sent = wire._kraken({"error": [], "result": {"count": 1}})  # noqa: SLF001

        assert await gw.cancel_order("XBTUSD", "OTXID-1") is True
        assert sent[0].url.path == "/0/private/CancelOrder"

    async def test_core_cancels_during_the_backoff_window(
        self, core: Any, rest: FakeKrakenRest, seen: Recorder
    ) -> None:
        cid = await _accepted(core, rest, seen)
        await _rejected(core, rest, seen, CANCEL_ONLY)  # window is now open

        core.cancel(_PAIR, cid)
        await seen.wait(CANCELLED)

        assert len(rest.bodies("cancel_order")) == 1
        assert [e.order_id for e in seen.of(CANCELLED)] == [cid]


# ---------------------------------------------------------------------------
# C. Backoff
# ---------------------------------------------------------------------------


class TestCBackoff:
    async def test_order_inside_the_window_fails_fast_without_http(
        self, core: Any, rest: FakeKrakenRest, seen: Recorder, clock: FakeClock
    ) -> None:
        await _rejected(core, rest, seen, CANCEL_ONLY)
        assert len(rest.bodies("add_order")) == 1

        clock.now += _BACKOFF - 1
        rest.add_order_errors = []  # the exchange would accept now; the core must not ask
        cid = _buy(core)
        await seen.wait(FAILURE, 2)

        assert len(rest.bodies("add_order")) == 1, "HTTP call made inside the backoff window"
        assert seen.of(FAILURE)[-1].order_id == cid
        assert seen.of(BUY_CREATED) == []

    async def test_order_after_the_window_is_submitted_again(
        self, core: Any, rest: FakeKrakenRest, seen: Recorder, clock: FakeClock
    ) -> None:
        await _rejected(core, rest, seen, CANCEL_ONLY)

        clock.now += _BACKOFF + 1
        await _accepted(core, rest, seen)

        assert len(rest.bodies("add_order")) == 2

    async def test_window_doubles_on_repeated_cancel_only(
        self, core: Any, rest: FakeKrakenRest, seen: Recorder, clock: FakeClock
    ) -> None:
        await _rejected(core, rest, seen, CANCEL_ONLY)  # window 10
        clock.now += _BACKOFF + 1
        await _rejected(core, rest, seen, CANCEL_ONLY)  # reaches Kraken, window 20
        assert len(rest.bodies("add_order")) == 2

        clock.now += 2 * _BACKOFF - 1  # inside the doubled window
        rest.add_order_errors = []
        _buy(core)
        await seen.wait(FAILURE, 3)
        assert len(rest.bodies("add_order")) == 2, "window did not double"

        clock.now += 2  # past the doubled window
        await _accepted(core, rest, seen)
        assert len(rest.bodies("add_order")) == 3

    async def test_window_resets_after_a_successful_order(
        self, core: Any, rest: FakeKrakenRest, seen: Recorder, clock: FakeClock
    ) -> None:
        await _rejected(core, rest, seen, CANCEL_ONLY)  # window 10
        clock.now += _BACKOFF + 1
        await _rejected(core, rest, seen, CANCEL_ONLY)  # window 20
        clock.now += 2 * _BACKOFF + 1
        await _accepted(core, rest, seen)  # success resets the growth
        sent_before = len(rest.bodies("add_order"))

        await _rejected(core, rest, seen, CANCEL_ONLY)  # window back to 10
        clock.now += _BACKOFF + 1
        await _accepted(core, rest, seen)

        assert len(rest.bodies("add_order")) == sent_before + 2

    async def test_post_only_does_not_open_a_cancel_only_window(
        self, core: Any, rest: FakeKrakenRest, seen: Recorder
    ) -> None:
        await _rejected(core, rest, seen, POST_ONLY)
        await _rejected(core, rest, seen, POST_ONLY)

        assert len(rest.bodies("add_order")) == 2


# ---------------------------------------------------------------------------
# D. Definite rejection, never UNKNOWN
# ---------------------------------------------------------------------------


class TestDDefiniteRejection:
    async def test_cancel_only_rejection_fails_the_order(
        self, core: Any, rest: FakeKrakenRest, seen: Recorder
    ) -> None:
        cid = await _rejected(core, rest, seen, CANCEL_ONLY)

        assert [f.order_id for f in seen.of(FAILURE)] == [cid]
        assert cid not in core.in_flight_orders
        assert core.fetch_order(cid).current_state.name == "FAILED"
        assert seen.of(BUY_CREATED) == []
        assert rest.bodies("open_orders") == []  # nothing to recover: it was not placed
        assert rest.bodies("closed_orders") == []

    async def test_fast_failed_order_inside_the_window_is_failed_not_unknown(
        self, core: Any, rest: FakeKrakenRest, seen: Recorder
    ) -> None:
        await _rejected(core, rest, seen, CANCEL_ONLY)
        rest.add_order_errors = []
        cid = _buy(core)
        await seen.wait(FAILURE, 2)

        assert cid not in core.in_flight_orders
        assert core.fetch_order(cid).current_state.name == "FAILED"


# ---------------------------------------------------------------------------
# E. Regression: ordinary unavailability is unchanged
# ---------------------------------------------------------------------------


class TestERegression:
    async def test_plain_unavailable_still_maps_to_service_unavailable_error(self) -> None:
        with pytest.raises(ServiceUnavailableError) as info:
            await _place_via_gateway(UNAVAILABLE)

        assert type(info.value) is ServiceUnavailableError
        assert not isinstance(info.value, _cancel_only_cls())

    def test_error_code_mapping_keeps_unavailable_and_busy(self) -> None:
        assert ERROR_CODE_MAPPING[UNAVAILABLE] is ServiceUnavailableError
        assert ERROR_CODE_MAPPING["EService:Busy"] is ServiceUnavailableError

    async def test_plain_unavailable_does_not_open_a_backoff_window(
        self, core: Any, rest: FakeKrakenRest, seen: Recorder
    ) -> None:
        await _rejected(core, rest, seen, UNAVAILABLE)
        await _rejected(core, rest, seen, UNAVAILABLE)

        assert len(rest.bodies("add_order")) == 2
