"""Abstract contract test base for gateway conformance.

Connector packages subclass this and provide a gateway fixture.
The contract tests validate that the gateway correctly implements
the ExchangeGateway protocol.

Also provides 5 conformance suite classes for framework abstractions:
  - SignerConformance
  - WsShapeDecoderConformance
  - WsAuthModelConformance
  - SymbolMapperConformance
  - RateLimitConformance
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest

from market_connector.primitives import OrderBookSnapshot

if TYPE_CHECKING:
    from market_connector.auth.protocols import Request, Signer
    from market_connector.protocols import ExchangeGateway
    from market_connector.rate_limits.flat import RateLimit
    from market_connector.symbols.mapper import SymbolMapper
    from market_connector.ws_models.auth_models import WsAuthModel
    from market_connector.ws_models.decoder import NormalizedWsMessage, WsShapeDecoder


class GatewayContractTestBase:
    """Abstract base: subclass and provide `gateway` and `trading_pair` fixtures.

    Each test method validates one aspect of the ExchangeGateway contract.
    Trading pair format validation (BASE-QUOTE canonical) is a per-connector
    responsibility, not tested here.
    """

    @pytest.fixture
    def gateway(self) -> ExchangeGateway:
        raise NotImplementedError("Subclass must provide a gateway fixture")

    @pytest.fixture
    def trading_pair(self) -> str:
        raise NotImplementedError("Subclass must provide a trading_pair fixture")

    # --- Lifecycle ---

    @pytest.mark.asyncio
    async def test_start_sets_ready(self, gateway: ExchangeGateway) -> None:
        assert not gateway.ready
        await gateway.start()
        assert gateway.ready

    @pytest.mark.asyncio
    async def test_stop_clears_ready(self, gateway: ExchangeGateway) -> None:
        """stop() MUST set ready=False per spec Connection Lifecycle section."""
        await gateway.start()
        await gateway.stop()
        assert not gateway.ready

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self, gateway: ExchangeGateway) -> None:
        await gateway.start()
        await gateway.stop()
        await gateway.stop()  # should not raise

    @pytest.mark.asyncio
    async def test_method_before_start_raises(
        self,
        gateway: ExchangeGateway,
        trading_pair: str,
    ) -> None:
        """Any gateway method called before start() must raise GatewayNotStartedError."""
        from market_connector.exceptions import GatewayNotStartedError

        with pytest.raises(GatewayNotStartedError):
            await gateway.place_order(trading_pair, "LIMIT", "BUY", Decimal("1"), Decimal("50000"))

    # --- Execution ---

    @pytest.mark.asyncio
    async def test_place_order_returns_client_id(
        self,
        gateway: ExchangeGateway,
        trading_pair: str,
    ) -> None:
        await gateway.start()
        order_id = await gateway.place_order(
            trading_pair,
            "LIMIT",
            "BUY",
            Decimal("1.0"),
            Decimal("50000"),
        )
        assert isinstance(order_id, str)
        assert len(order_id) > 0

    @pytest.mark.asyncio
    async def test_cancel_order_returns_bool(
        self,
        gateway: ExchangeGateway,
        trading_pair: str,
    ) -> None:
        await gateway.start()
        order_id = await gateway.place_order(
            trading_pair,
            "LIMIT",
            "BUY",
            Decimal("1.0"),
            Decimal("50000"),
        )
        result = await gateway.cancel_order(trading_pair, order_id)
        assert isinstance(result, bool)

    @pytest.mark.asyncio
    async def test_get_open_orders_returns_list(
        self,
        gateway: ExchangeGateway,
        trading_pair: str,
    ) -> None:
        await gateway.start()
        orders = await gateway.get_open_orders(trading_pair)
        assert isinstance(orders, list)

    @pytest.mark.asyncio
    async def test_get_balance_returns_decimal(
        self,
        gateway: ExchangeGateway,
    ) -> None:
        await gateway.start()
        balance = await gateway.get_balance("USDT")
        assert isinstance(balance, Decimal)

    # --- Market Data ---

    @pytest.mark.asyncio
    async def test_get_orderbook_returns_snapshot(
        self,
        gateway: ExchangeGateway,
        trading_pair: str,
    ) -> None:
        await gateway.start()
        book = await gateway.get_orderbook(trading_pair)
        assert isinstance(book, OrderBookSnapshot)
        assert book.trading_pair == trading_pair

    @pytest.mark.asyncio
    async def test_get_mid_price_returns_decimal(
        self,
        gateway: ExchangeGateway,
        trading_pair: str,
    ) -> None:
        await gateway.start()
        price = await gateway.get_mid_price(trading_pair)
        assert isinstance(price, Decimal)
        assert price > 0

    @pytest.mark.asyncio
    async def test_get_candles_returns_list(
        self,
        gateway: ExchangeGateway,
        trading_pair: str,
    ) -> None:
        await gateway.start()
        candles = await gateway.get_candles(trading_pair, "1m", 10)
        assert isinstance(candles, list)


# ---------------------------------------------------------------------------
# SignerConformance
# ---------------------------------------------------------------------------


class SignerConformance:
    """Validates that a Signer produces the expected signed Request fields.

    Accepts a Signer instance directly for maximum flexibility.  Only the
    fields present in ``expected_output`` are checked — omit a field to skip
    its validation.

    Args:
        signer: A concrete Signer instance (must have an async ``sign`` method).
        fixture_request: The unsigned Request passed to ``signer.sign()``.
        expected_output: Dict with optional keys ``"headers"``, ``"body"``,
            ``"qs_params"``.  Only supplied keys are asserted.

    Usage (inside an async test)::

        await SignerConformance(signer, request, {"headers": {"X-Sig": "abc"}}).run()

    ``run()`` is a coroutine — always call it with ``await``.
    """

    def __init__(
        self,
        signer: Signer,
        fixture_request: Request,
        expected_output: dict[str, Any],
    ) -> None:
        self._signer = signer
        self._request = fixture_request
        self._expected = expected_output

    async def run(self) -> None:
        """Sign the fixture request and assert produced fields match expected_output.

        Must be called with ``await`` from an async test.
        """
        signed = await self._signer.sign(self._request)
        if "headers" in self._expected:
            for key, value in self._expected["headers"].items():
                assert signed.headers.get(key) == value, (
                    f"Header {key!r}: expected {value!r}, got {signed.headers.get(key)!r}"
                )
        if "body" in self._expected:
            assert signed.body == self._expected["body"], (
                f"body: expected {self._expected['body']!r}, got {signed.body!r}"
            )
        if "qs_params" in self._expected:
            for key, value in self._expected["qs_params"].items():
                assert signed.qs_params.get(key) == value, (
                    f"qs_params[{key!r}]: expected {value!r}, got {signed.qs_params.get(key)!r}"
                )


# ---------------------------------------------------------------------------
# WsShapeDecoderConformance
# ---------------------------------------------------------------------------


class WsShapeDecoderConformance:
    """Validates that a WsShapeDecoder produces the expected NormalizedWsMessage.

    Args:
        decoder: Concrete decoder instance (JsonEnvelopeDecoder, PositionalArrayDecoder, …).
        fixture_frames: List of ``(raw_frame, expected_NormalizedWsMessage)`` pairs.
            Each pair is decoded and compared for equality.

    Usage::

        WsShapeDecoderConformance(decoder, [(raw, expected)]).run()
    """

    def __init__(
        self,
        decoder: WsShapeDecoder,
        fixture_frames: list[tuple[Any, NormalizedWsMessage]],
    ) -> None:
        self._decoder = decoder
        self._frames = fixture_frames

    def run(self) -> None:
        """Decode each frame and assert equality with the expected NormalizedWsMessage."""
        for i, (raw, expected) in enumerate(self._frames):
            actual = self._decoder.decode(raw)
            assert actual == expected, f"Frame {i}: expected {expected!r}, got {actual!r}"


# ---------------------------------------------------------------------------
# WsAuthModelConformance
# ---------------------------------------------------------------------------


class WsAuthModelConformance:
    """Validates the 4 lifecycle hooks of a WsAuthModel.

    Drives all 4 hooks and validates ``transform_outgoing`` produces the
    expected dict.  ``on_connected`` and ``refresh`` are validated for
    no-exception semantics only.

    Args:
        model: Concrete WsAuthModel instance.
        expected_transform_output: Expected dict returned by
            ``transform_outgoing(sample_msg)``.
        sample_msg: Input message passed to ``transform_outgoing``.

    Usage (inside an async test)::

        await WsAuthModelConformance(model, expected_output, sample_msg).run()
    """

    def __init__(
        self,
        model: WsAuthModel,
        expected_transform_output: dict,
        sample_msg: dict,
    ) -> None:
        self._model = model
        self._expected_transform = expected_transform_output
        self._sample_msg = sample_msg

    async def run(self) -> None:
        """Drive all 4 hooks; assert transform_outgoing output matches expected."""
        base_url = "wss://stream.example.com/ws"

        # Hook 1: prepare_connection (may rewrite URL — no exception required)
        await self._model.prepare_connection(base_url)

        # Hook 2: on_connected (may send messages — use AsyncMock send callable)
        mock_send = AsyncMock()
        await self._model.on_connected(mock_send)

        # Hook 3: transform_outgoing — must return expected dict
        actual = await self._model.transform_outgoing(self._sample_msg)
        assert actual == self._expected_transform, (
            f"transform_outgoing: expected {self._expected_transform!r}, got {actual!r}"
        )

        # Hook 4: refresh (no exception required)
        await self._model.refresh()


# ---------------------------------------------------------------------------
# SymbolMapperConformance
# ---------------------------------------------------------------------------


class SymbolMapperConformance:
    """Validates bidirectional round-trip correctness of a SymbolMapper.

    For each ``(hb_pair, exch_pair)`` fixture pair, asserts:
    - ``mapper.to_exchange_pair(hb) == exch``
    - ``mapper.from_exchange_pair(exch) == hb``
    - Round-trip: ``from_exchange_pair(to_exchange_pair(hb)) == hb``
    - Round-trip: ``to_exchange_pair(from_exchange_pair(exch)) == exch``

    Args:
        mapper: Concrete SymbolMapper instance.
        fixture_pairs: List of ``(hb_canonical_pair, exchange_pair)`` tuples.

    Usage::

        SymbolMapperConformance(mapper, [("BTC-USD", "BTCUSD")]).run()
    """

    def __init__(
        self,
        mapper: SymbolMapper,
        fixture_pairs: list[tuple[str, str]],
    ) -> None:
        self._mapper = mapper
        self._pairs = fixture_pairs

    def run(self) -> None:
        """Assert all forward, reverse, and round-trip conversions match."""
        for i, (hb, exch) in enumerate(self._pairs):
            actual_exch = self._mapper.to_exchange_pair(hb)
            assert actual_exch == exch, (
                f"Pair {i}: to_exchange_pair({hb!r}) = {actual_exch!r}, expected {exch!r}"
            )

            actual_hb = self._mapper.from_exchange_pair(exch)
            assert actual_hb == hb, (
                f"Pair {i}: from_exchange_pair({exch!r}) = {actual_hb!r}, expected {hb!r}"
            )

            # Round-trip: hb → exch → hb
            rt_hb = self._mapper.from_exchange_pair(self._mapper.to_exchange_pair(hb))
            assert rt_hb == hb, f"Pair {i}: round-trip hb→exch→hb failed: {hb!r} → {rt_hb!r}"

            # Round-trip: exch → hb → exch
            rt_exch = self._mapper.to_exchange_pair(self._mapper.from_exchange_pair(exch))
            assert rt_exch == exch, (
                f"Pair {i}: round-trip exch→hb→exch failed: {exch!r} → {rt_exch!r}"
            )


# ---------------------------------------------------------------------------
# RateLimitConformance
# ---------------------------------------------------------------------------


class RateLimitConformance:
    """Validates that a RateLimit completes a request stream within a time budget.

    Fires all requests via ``acquire``, measures wall time, and asserts the
    total elapsed time is ``<= expected_max_duration_seconds``.  Timing
    tolerances are intentionally generous — keep ``expected_max_duration_seconds``
    ≥ 0.5 s above the theoretical minimum to avoid CI flakiness.

    Args:
        rate_limit: Concrete RateLimit instance.
        request_stream: List of ``(endpoint_name, weight)`` tuples.
        expected_max_duration_seconds: Wall-time ceiling in seconds.

    Usage (inside an async test)::

        await RateLimitConformance(limiter, [("ep", 1)] * 10, 2.0).run()
    """

    def __init__(
        self,
        rate_limit: RateLimit,
        request_stream: list[tuple[str, int]],
        expected_max_duration_seconds: float,
    ) -> None:
        self._limiter = rate_limit
        self._stream = request_stream
        self._max_duration = expected_max_duration_seconds

    async def run(self) -> None:
        """Acquire all requests and assert total elapsed ≤ expected_max_duration_seconds."""
        start = time.monotonic()
        for endpoint_name, weight in self._stream:
            await self._limiter.acquire(endpoint_name, weight)
        elapsed = time.monotonic() - start

        assert elapsed <= self._max_duration, (
            f"RateLimitConformance: elapsed {elapsed:.3f}s exceeded "
            f"ceiling {self._max_duration:.3f}s"
        )
