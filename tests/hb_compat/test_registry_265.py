# ruff: noqa: F811  (fixtures are imported from the #211 module and used as test parameters)
"""Exchange descriptor, registry, handle lifecycle, ConnectorCoreProtocol (issue #265), RED tests.

Pinned API (none of it exists yet; every import happens inside a test or fixture so the
file collects and each test fails on its own)::

    market_connector.hb_compat.registry
        list_exchanges() -> Sequence[ExchangeDescriptor]
        get_exchange(name) -> ExchangeDescriptor          # unknown name -> KeyError
    market_connector.hb_compat.core_protocol
        ConnectorCoreProtocol (runtime_checkable Protocol), ExchangeDescriptor (frozen
        dataclass), ConfigKey (frozen: name, is_secret, prompt, required, default),
        MarketType (StrEnum SPOT/PERPETUAL), ConnectorCoreHandle
    market_connector.exchanges.kraken.hb_compat.descriptor.KRAKEN_DESCRIPTOR

ExchangeDescriptor fields: name, display_name, market_type, config_keys (tuple),
example_pair, default_maker_fee, default_taker_fee (Decimal fractions),
order_capabilities, has_core; method ``config_keys_with_prefix(prefix) -> tuple[ConfigKey]``
(``kraken_mc`` -> ``kraken_mc_api_key`` ...); coroutine
``create(config, *, event_bus, **core_options) -> ConnectorCoreHandle`` (no network I/O;
missing required key -> ValueError; invalid tier -> ValueError; tier is case-insensitive).

ConnectorCoreHandle: ``.core .gateway .event_bus .descriptor``; ``start()`` starts the
gateway THEN the core; ``close()`` stops the core THEN the gateway and is idempotent;
async context manager; ``start()`` after ``close()`` -> RuntimeError.

L0 types (top-level ``data_type_primitives``): ``core.trading_rules`` is
``Mapping[str, TradingRules]`` (min_price_increment = 10**-price_decimals,
min_base_amount_increment = 10**-amount_decimals; a 0 increment means no constraint);
``get_order_book`` returns ``OrderBookSnapshot | None`` (int ms timestamp, best-first levels).
New core methods: quantize_order_price (round DOWN), get_order_price_quantum,
get_order_size_quantum.
"""

from __future__ import annotations

import dataclasses
import importlib
import sys
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from data_type_primitives import OrderBookSnapshot, TradingRules

from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway
from market_connector.exchanges.kraken.mixins.orders import KRAKEN_ORDER_CAPABILITIES
from market_connector.exchanges.kraken.schemas.enums import KrakenAPITier
from market_connector.exchanges.kraken.transport import KrakenTransport
from market_connector.transport.ws_base import WsConnectorBase
from tests.exchanges.kraken.unit.test_kraken_connector_core_211 import (  # noqa: F401
    Recorder,
    bare_connector,
    bus,
    connector,
    gateway,
    rest,
    seen,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from market_connector.hb_compat.event_bus import EventBus
    from tests.exchanges.kraken.unit.test_kraken_connector_core_211 import FakeKrakenRest

_CORE_PROTOCOL = "market_connector.hb_compat.core_protocol"
_REGISTRY = "market_connector.hb_compat.registry"
_KRAKEN_DESCRIPTOR = "market_connector.exchanges.kraken.hb_compat.descriptor"

_CONFIG = {"kraken_api_key": "key", "kraken_secret_key": "c2VjcmV0"}

_PROTOCOL_MEMBERS = [
    "buy",
    "sell",
    "cancel",
    "fetch_order",
    "client_order_id_for_txid",
    "txid_for_client_order_id",
    "get_price_by_type",
    "get_order_book",
    "quantize_order_amount",
    "quantize_order_price",
    "get_order_price_quantum",
    "get_order_size_quantum",
    "get_balance",
    "get_available_balance",
    "trading_rules",
    "in_flight_orders",
    "status_dict",
    "ready",
    "start",
    "stop",
    "update_order_status",
    "update_prices",
    "update_order_book",
    "update_trading_rules",
    "update_balances",
]


def _core_protocol() -> Any:
    return importlib.import_module(_CORE_PROTOCOL)


def _registry() -> Any:
    return importlib.import_module(_REGISTRY)


def _kraken() -> Any:
    return importlib.import_module(_KRAKEN_DESCRIPTOR).KRAKEN_DESCRIPTOR


@dataclasses.dataclass
class _NoIo:
    request: AsyncMock
    ws_connect: AsyncMock
    gateway_start: AsyncMock

    def assert_untouched(self) -> None:
        self.request.assert_not_awaited()
        self.ws_connect.assert_not_awaited()
        self.gateway_start.assert_not_awaited()


@pytest.fixture
def no_io() -> Iterator[_NoIo]:
    """Patch every network entry point; create() must not touch any of them."""
    with (
        patch.object(KrakenTransport, "request", new_callable=AsyncMock) as request,
        patch.object(WsConnectorBase, "connect", new_callable=AsyncMock) as ws_connect,
        patch.object(KrakenGateway, "start", new_callable=AsyncMock) as gw_start,
    ):
        yield _NoIo(request, ws_connect, gw_start)


@pytest.fixture
async def handle(no_io: _NoIo, bus: EventBus) -> Any:
    return await _kraken().create(_CONFIG, event_bus=bus)


@pytest.fixture
def calls(handle: Any) -> list[str]:
    """Record start/stop order across the handle's gateway and core."""
    order: list[str] = []
    for owner, label in ((handle.gateway, "gateway"), (handle.core, "core")):
        for method in ("start", "stop"):
            mock = AsyncMock(side_effect=lambda name=f"{label}.{method}": order.append(name))
            patch.object(owner, method, mock).start()
    return order


@pytest.fixture(autouse=True)
def _stop_patches() -> Iterator[None]:
    yield
    patch.stopall()


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_list_exchanges_contains_kraken(self) -> None:
        exchanges = _registry().list_exchanges()
        assert any(d is _kraken() for d in exchanges)
        assert "kraken" in [d.name for d in exchanges]

    def test_get_exchange_returns_the_kraken_descriptor(self) -> None:
        assert _registry().get_exchange("kraken") is _kraken()

    def test_unknown_exchange_raises_key_error(self) -> None:
        with pytest.raises(KeyError):
            _registry().get_exchange("no-such-exchange")

    def test_registry_import_pulls_in_no_hummingbot(self) -> None:
        before = {m for m in sys.modules if m.split(".")[0] == "hummingbot"}
        sys.modules.pop(_REGISTRY, None)
        importlib.import_module(_REGISTRY)
        _registry().get_exchange("kraken")
        after = {m for m in sys.modules if m.split(".")[0] == "hummingbot"}
        assert after == before


# ---------------------------------------------------------------------------
# core_protocol types
# ---------------------------------------------------------------------------


class TestCoreProtocolTypes:
    def test_market_type_is_a_str_enum(self) -> None:
        market_type = _core_protocol().MarketType
        assert market_type.SPOT == "spot" or market_type.SPOT.value == market_type.SPOT
        assert {m.name for m in market_type} == {"SPOT", "PERPETUAL"}
        assert isinstance(market_type.SPOT, str)

    def test_config_key_is_frozen_with_pinned_fields(self) -> None:
        config_key = _core_protocol().ConfigKey
        assert dataclasses.is_dataclass(config_key)
        assert [f.name for f in dataclasses.fields(config_key)] == [
            "name",
            "is_secret",
            "prompt",
            "required",
            "default",
        ]
        key = config_key(name="n", is_secret=False, prompt="p", required=True, default=None)
        with pytest.raises(dataclasses.FrozenInstanceError):
            key.name = "other"

    def test_exchange_descriptor_is_a_frozen_dataclass(self) -> None:
        descriptor = _kraken()
        assert isinstance(descriptor, _core_protocol().ExchangeDescriptor)
        assert dataclasses.is_dataclass(descriptor)
        with pytest.raises(dataclasses.FrozenInstanceError):
            descriptor.name = "other"

    @pytest.mark.parametrize("member", _PROTOCOL_MEMBERS)
    def test_protocol_declares_member(self, member: str) -> None:
        protocol = _core_protocol().ConnectorCoreProtocol
        declared = set(getattr(protocol, "__protocol_attrs__", ())) | set(dir(protocol))
        assert member in declared

    def test_protocol_is_runtime_checkable(self) -> None:
        protocol = _core_protocol().ConnectorCoreProtocol
        assert getattr(protocol, "_is_protocol", False)
        assert getattr(protocol, "_is_runtime_protocol", False)


# ---------------------------------------------------------------------------
# Kraken descriptor values
# ---------------------------------------------------------------------------


class TestKrakenDescriptor:
    def test_scalar_fields(self) -> None:
        d = _kraken()
        assert d.name == "kraken"
        assert d.display_name == "Kraken"
        assert d.market_type is _core_protocol().MarketType.SPOT
        assert d.example_pair == "ETH-USDC"
        assert d.has_core is True

    def test_default_fees(self) -> None:
        d = _kraken()
        assert d.default_maker_fee == Decimal("0.0025")
        assert d.default_taker_fee == Decimal("0.004")
        assert isinstance(d.default_maker_fee, Decimal)
        assert isinstance(d.default_taker_fee, Decimal)

    def test_order_capabilities(self) -> None:
        assert _kraken().order_capabilities == KRAKEN_ORDER_CAPABILITIES

    def test_config_keys_in_order(self) -> None:
        config_key = _core_protocol().ConfigKey
        assert tuple(_kraken().config_keys) == (
            config_key(
                name="kraken_api_key",
                is_secret=True,
                prompt="Enter your Kraken API key",
                required=True,
                default=None,
            ),
            config_key(
                name="kraken_secret_key",
                is_secret=True,
                prompt="Enter your Kraken secret key",
                required=True,
                default=None,
            ),
            config_key(
                name="kraken_api_tier",
                is_secret=False,
                prompt="Enter your Kraken API Tier (Starter/Intermediate/Pro)",
                required=False,
                default="Starter",
            ),
        )

    def test_config_keys_with_prefix(self) -> None:
        d = _kraken()
        derived = d.config_keys_with_prefix("kraken_mc")
        assert [k.name for k in derived] == [
            "kraken_mc_api_key",
            "kraken_mc_secret_key",
            "kraken_mc_api_tier",
        ]
        for original, new in zip(d.config_keys, derived, strict=True):
            assert new == dataclasses.replace(original, name=new.name)

    def test_config_keys_with_prefix_leaves_descriptor_unchanged(self) -> None:
        d = _kraken()
        before = tuple(d.config_keys)
        d.config_keys_with_prefix("kraken_mc")
        assert tuple(d.config_keys) == before


# ---------------------------------------------------------------------------
# create(): no I/O, config handling, core options
# ---------------------------------------------------------------------------


class TestCreate:
    async def test_create_does_no_network_io(self, no_io: _NoIo, bus: EventBus) -> None:
        await _kraken().create(_CONFIG, event_bus=bus)
        no_io.assert_untouched()

    async def test_handle_exposes_core_gateway_bus_descriptor(
        self, handle: Any, bus: EventBus
    ) -> None:
        assert isinstance(handle, _core_protocol().ConnectorCoreHandle)
        assert isinstance(handle.gateway, KrakenGateway)
        assert handle.event_bus is bus
        assert handle.descriptor is _kraken()
        assert handle.core is not None

    async def test_core_conforms_to_protocol(self, handle: Any) -> None:
        assert isinstance(handle.core, _core_protocol().ConnectorCoreProtocol)

    async def test_default_tier_is_starter(self, handle: Any) -> None:
        assert handle.gateway._config.tier == KrakenAPITier.STARTER

    @pytest.mark.parametrize("tier", ["pro", "PRO", "Pro"])
    async def test_tier_is_case_insensitive(self, no_io: _NoIo, bus: EventBus, tier: str) -> None:
        h = await _kraken().create({**_CONFIG, "kraken_api_tier": tier}, event_bus=bus)
        assert h.gateway._config.tier == KrakenAPITier.PRO

    async def test_invalid_tier_raises_value_error(self, no_io: _NoIo, bus: EventBus) -> None:
        with pytest.raises(ValueError):  # noqa: PT011
            await _kraken().create({**_CONFIG, "kraken_api_tier": "Platinum"}, event_bus=bus)

    @pytest.mark.parametrize("missing", ["kraken_api_key", "kraken_secret_key"])
    async def test_missing_required_key_raises_value_error(
        self, no_io: _NoIo, bus: EventBus, missing: str
    ) -> None:
        config = {k: v for k, v in _CONFIG.items() if k != missing}
        with pytest.raises(ValueError, match=missing):
            await _kraken().create(config, event_bus=bus)
        no_io.assert_untouched()

    async def test_core_options_pass_through(self, no_io: _NoIo, bus: EventBus) -> None:
        h = await _kraken().create(
            _CONFIG, event_bus=bus, status_poll_interval=2.5, unknown_order_grace=7.0
        )
        assert h.core._poll_interval == 2.5
        assert h.core._unknown_grace == 7.0


# ---------------------------------------------------------------------------
# handle lifecycle
# ---------------------------------------------------------------------------


class TestHandleLifecycle:
    async def test_start_starts_gateway_then_core(self, handle: Any, calls: list[str]) -> None:
        await handle.start()
        assert calls == ["gateway.start", "core.start"]

    async def test_close_stops_core_then_gateway(self, handle: Any, calls: list[str]) -> None:
        await handle.start()
        calls.clear()
        await handle.close()
        assert calls == ["core.stop", "gateway.stop"]

    async def test_close_is_idempotent(self, handle: Any, calls: list[str]) -> None:
        await handle.start()
        calls.clear()
        await handle.close()
        await handle.close()
        assert calls == ["core.stop", "gateway.stop"]

    async def test_async_context_manager_starts_and_closes(
        self, handle: Any, calls: list[str]
    ) -> None:
        async with handle as entered:
            assert entered is handle
            assert calls == ["gateway.start", "core.start"]
        assert calls == ["gateway.start", "core.start", "core.stop", "gateway.stop"]

    async def test_context_manager_closes_on_error(self, handle: Any, calls: list[str]) -> None:
        with pytest.raises(RuntimeError, match="boom"):
            async with handle:
                raise RuntimeError("boom")
        assert calls[-2:] == ["core.stop", "gateway.stop"]

    async def test_start_after_close_raises_runtime_error(
        self, handle: Any, calls: list[str]
    ) -> None:
        await handle.start()
        await handle.close()
        with pytest.raises(RuntimeError):
            await handle.start()


# ---------------------------------------------------------------------------
# L0 types and quantization on the Kraken core
# ---------------------------------------------------------------------------


class TestL0Types:
    async def test_trading_rules_are_dtp_trading_rules(self, connector: Any) -> None:
        rules = connector.trading_rules
        assert {"BTC-USD", "ETH-USD", "SOL-USD"} <= set(rules)
        assert all(isinstance(r, TradingRules) for r in rules.values())

    async def test_increments_derive_from_decimals(self, connector: Any) -> None:
        btc = connector.trading_rules["BTC-USD"]  # pair_decimals=1, lot_decimals=8
        assert btc.trading_pair == "BTC-USD"
        assert btc.min_price_increment == Decimal("0.1")
        assert btc.min_base_amount_increment == Decimal("0.00000001")
        eth = connector.trading_rules["ETH-USD"]  # pair_decimals=2, lot_decimals=4
        assert eth.min_price_increment == Decimal("0.01")
        assert eth.min_base_amount_increment == Decimal("0.0001")

    async def test_min_sizes_carried_over(self, connector: Any) -> None:
        btc = connector.trading_rules["BTC-USD"]
        assert btc.min_order_size == Decimal("0.0001")
        assert btc.min_notional_size == Decimal("0.5")
        assert connector.trading_rules["SOL-USD"].min_notional_size == Decimal(5)

    async def test_get_order_book_returns_dtp_snapshot_or_none(
        self, connector: Any, rest: FakeKrakenRest
    ) -> None:
        assert connector.get_order_book("BTC-USD") is None
        await connector.update_order_book("BTC-USD")
        book = connector.get_order_book("BTC-USD")
        assert book is None or isinstance(book, OrderBookSnapshot)
        if book is not None:
            assert isinstance(book.timestamp, int)  # Unix milliseconds
            bids = [e.price for e in book.bids]
            asks = [e.price for e in book.asks]
            assert bids == sorted(bids, reverse=True)  # best first
            assert asks == sorted(asks)


class TestQuantization:
    @pytest.mark.parametrize(
        ("pair", "price", "expected"),
        [
            ("BTC-USD", "27000.19", "27000.1"),  # price_decimals=1, rounds DOWN
            ("BTC-USD", "27000.1", "27000.1"),
            ("BTC-USD", "27000.99", "27000.9"),
            ("BTC-USD", "0.09", "0"),
            ("ETH-USD", "1800.129", "1800.12"),  # price_decimals=2
        ],
    )
    async def test_quantize_order_price_rounds_down(
        self, connector: Any, pair: str, price: str, expected: str
    ) -> None:
        result = connector.quantize_order_price(pair, Decimal(price))
        assert isinstance(result, Decimal)
        assert result == Decimal(expected)

    async def test_price_quantum_is_min_price_increment(self, connector: Any) -> None:
        for pair, quantum in (("BTC-USD", "0.1"), ("ETH-USD", "0.01")):
            result = connector.get_order_price_quantum(pair, Decimal("27000.19"))
            assert result == Decimal(quantum)
            assert result == connector.trading_rules[pair].min_price_increment

    async def test_size_quantum_is_min_base_amount_increment(self, connector: Any) -> None:
        for pair, quantum in (("BTC-USD", "0.00000001"), ("ETH-USD", "0.0001")):
            result = connector.get_order_size_quantum(pair, Decimal("0.5"))
            assert result == Decimal(quantum)
            assert result == connector.trading_rules[pair].min_base_amount_increment

    async def test_quantize_order_amount_stays_consistent_with_quantum(
        self, connector: Any
    ) -> None:
        amount = Decimal("0.123456789")
        quantum = connector.get_order_size_quantum("BTC-USD", amount)
        result = connector.quantize_order_amount("BTC-USD", amount)
        assert result == Decimal("0.12345678")
        assert result % quantum == 0
        assert result <= amount < result + quantum

    async def test_zero_price_increment_means_no_constraint(self, connector: Any) -> None:
        connector.trading_rules["BTC-USD"] = TradingRules(
            trading_pair="BTC-USD", min_price_increment=Decimal(0)
        )
        price = Decimal("27000.123456789")
        assert connector.quantize_order_price("BTC-USD", price) == price

    async def test_zero_amount_increment_means_no_constraint(self, connector: Any) -> None:
        connector.trading_rules["BTC-USD"] = TradingRules(
            trading_pair="BTC-USD", min_base_amount_increment=Decimal(0)
        )
        amount = Decimal("0.123456789123")
        assert connector.quantize_order_amount("BTC-USD", amount) == amount

    async def test_quantized_price_is_a_multiple_of_the_quantum(self, connector: Any) -> None:
        price = Decimal("27000.19")
        quantum = connector.get_order_price_quantum("BTC-USD", price)
        result = connector.quantize_order_price("BTC-USD", price)
        assert result % quantum == 0
        assert result <= price < result + quantum


# ---------------------------------------------------------------------------
# review follow-ups
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _Mocks:
    gw_start: AsyncMock
    gw_stop: AsyncMock
    core_start: AsyncMock
    core_stop: AsyncMock


def _install_mocks(
    handle: Any, *, gw_start_exc: Exception | None = None, core_start_exc: Exception | None = None
) -> _Mocks:
    mocks = _Mocks(
        AsyncMock(side_effect=gw_start_exc),
        AsyncMock(),
        AsyncMock(side_effect=core_start_exc),
        AsyncMock(),
    )
    patch.object(handle.gateway, "start", mocks.gw_start).start()
    patch.object(handle.gateway, "stop", mocks.gw_stop).start()
    patch.object(handle.core, "start", mocks.core_start).start()
    patch.object(handle.core, "stop", mocks.core_stop).start()
    return mocks


class TestStartRollback:
    async def test_gateway_start_failure_rolls_back(self, handle: Any) -> None:
        mocks = _install_mocks(handle, gw_start_exc=OSError("boom"))
        with pytest.raises(OSError, match="boom"):
            await handle.start()
        mocks.core_start.assert_not_awaited()
        mocks.gw_stop.assert_awaited_once()
        await handle.close()  # no-op after the rollback
        mocks.gw_stop.assert_awaited_once()
        mocks.core_stop.assert_not_awaited()

    async def test_core_start_failure_rolls_back(self, handle: Any) -> None:
        mocks = _install_mocks(handle, core_start_exc=OSError("core boom"))
        with pytest.raises(OSError, match="core boom"):
            await handle.start()
        mocks.gw_stop.assert_awaited_once()
        await handle.close()
        mocks.gw_stop.assert_awaited_once()

    async def test_context_manager_failing_start_rolls_back(self, handle: Any) -> None:
        mocks = _install_mocks(handle, gw_start_exc=OSError("boom"))
        with pytest.raises(OSError, match="boom"):
            async with handle:
                pytest.fail("body must not run")
        mocks.core_start.assert_not_awaited()
        mocks.gw_stop.assert_awaited_once()


class TestGatewayPartialStart:
    async def test_stop_closes_rest_after_failed_ws_connect(self) -> None:
        gw = KrakenGateway(api_key="k", secret_key="c2VjcmV0")
        with (
            patch.object(KrakenTransport, "request", new_callable=AsyncMock),
            patch.object(
                WsConnectorBase, "connect", new_callable=AsyncMock, side_effect=OSError("ws")
            ),
            patch.object(KrakenTransport, "close", new_callable=AsyncMock) as rest_close,
        ):
            with pytest.raises(OSError, match="ws"):
                await gw.start()
            await gw.stop()
            rest_close.assert_awaited()


class TestSecretRepr:
    def test_config_repr_hides_secrets(self) -> None:
        from market_connector.exchanges.kraken.kraken_gateway import KrakenConfig

        text = repr(KrakenConfig(api_key="KEYSECRET", secret_key="SSECRET"))
        assert "KEYSECRET" not in text
        assert "SSECRET" not in text

    def test_gateway_config_repr_hides_secrets(self) -> None:
        gw = KrakenGateway(api_key="KEYSECRET", secret_key="c1NTRUNSRVQ=")
        text = repr(gw._config)
        assert "KEYSECRET" not in text
        assert "c1NTRUNSRVQ=" not in text


class TestQuantizePrecision:
    async def test_amount_with_more_than_28_digits_truncates_down(self, connector: Any) -> None:
        amount = Decimal("0." + "9" * 32)
        assert connector.quantize_order_amount("BTC-USD", amount) == Decimal("0.99999999")

    async def test_price_with_more_than_28_digits_truncates_down(self, connector: Any) -> None:
        price = Decimal("0." + "9" * 32)
        assert connector.quantize_order_price("ETH-USD", price) == Decimal("0.99")

    @pytest.mark.parametrize(
        ("amount", "expected"),
        [
            ("1.5", "1.5"),
            ("100", "100"),
            ("1.50000000", "1.50000000"),
            ("1.500000000", "1.50000000"),
        ],
    )
    async def test_amount_string_form_matches_legacy(
        self, connector: Any, amount: str, expected: str
    ) -> None:
        assert str(connector.quantize_order_amount("BTC-USD", Decimal(amount))) == expected

    def test_nan_increment_returns_value_unchanged(self) -> None:
        from market_connector.exchanges.kraken.hb_compat.kraken_core_support import round_down

        value = Decimal("1.2345")
        assert round_down(value, Decimal("NaN")) == value


class TestLazyRegistryLookup:
    def test_get_exchange_kraken(self) -> None:
        assert _registry().get_exchange("kraken").name == "kraken"

    def test_unknown_lookup_imports_no_exchange_module(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delitem(sys.modules, _KRAKEN_DESCRIPTOR, raising=False)
        with pytest.raises(KeyError):
            _registry().get_exchange("nope")
        assert _KRAKEN_DESCRIPTOR not in sys.modules
