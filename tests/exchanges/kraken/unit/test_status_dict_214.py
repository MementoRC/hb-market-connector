"""Kraken core ``status_dict`` readiness (issue #214 item 9), RED step.

Pinned contract (``KrakenConnectorCore``; nothing here exists yet):

* ``status_dict`` is a property returning a FRESH ``dict[str, bool]`` per call with exactly
  the keys in ``KEYS`` (hummingbot ``ExchangePyBase`` names where the subsystem exists):

  - ``symbols_mapping_initialized``: True once ``update_trading_rules`` succeeded (the
    hummingbot pair -> wire pair map is built there).
  - ``trading_rule_initialized``: True once ``update_trading_rules`` succeeded.
  - ``account_balance``: True once ``update_balances`` succeeded (first balance poll).
  - ``status_polling_initialized``: True while the poller started by ``start()`` is live;
    False before ``start()`` and after ``stop()``.

  Excluded: ``user_stream_initialized`` (the core never owns the WebSocket; ``on_own_trades``
  is fed from outside) and ``order_books_initialized`` (the core caches books on demand per
  pair and has no configured pair set to be complete against).
* ``ready`` is derived: ``ready == all(status_dict.values())``.
* A failed ``update_*`` leaves its key False (and does not raise through ``status_dict``).
"""

from __future__ import annotations

import importlib
from typing import Any

import httpx
import pytest

from market_connector.exchanges.kraken.kraken_gateway import KrakenGateway
from market_connector.hb_compat.event_bus import EventBus
from tests.exchanges.kraken.unit import test_kraken_connector_core_211 as base

_CORE_MOD = "market_connector.exchanges.kraken.hb_compat.kraken_connector_core"

KEYS = {
    "symbols_mapping_initialized",
    "trading_rule_initialized",
    "account_balance",
    "status_polling_initialized",
}
_RULE_KEYS = {"symbols_mapping_initialized", "trading_rule_initialized"}


@pytest.fixture
def gateway() -> KrakenGateway:
    gw = KrakenGateway(api_key="k", secret_key="c2VjcmV0")
    gw._rest = base.FakeKrakenRest()  # type: ignore[assignment]
    gw._started = True
    return gw


@pytest.fixture
def core(gateway: KrakenGateway) -> Any:
    cls = importlib.import_module(_CORE_MOD).KrakenConnectorCore
    return cls(gateway, event_bus=EventBus(name="status-214"), status_poll_interval=3600.0)


def _true_keys(core: Any) -> set[str]:
    return {k for k, v in core.status_dict.items() if v}


def _assert_ready_derived(core: Any) -> None:
    assert core.ready == all(core.status_dict.values())


def _fail(monkeypatch: pytest.MonkeyPatch, gateway: KrakenGateway, method: str) -> None:
    async def _boom(*_: Any, **__: Any) -> Any:
        raise httpx.ConnectError("down")

    monkeypatch.setattr(gateway, method, _boom)


def test_status_dict_is_a_property_with_agreed_keys(core: Any) -> None:
    assert isinstance(type(core).status_dict, property)
    status = core.status_dict
    assert isinstance(status, dict)
    assert set(status) == KEYS
    assert all(type(v) is bool for v in status.values())


async def test_everything_false_before_start(core: Any) -> None:
    assert not any(core.status_dict.values())
    assert core.ready is False
    _assert_ready_derived(core)


async def test_trading_rules_set_rule_and_mapping_keys_only(core: Any) -> None:
    await core.update_trading_rules()
    assert _true_keys(core) == _RULE_KEYS
    assert core.ready is False
    _assert_ready_derived(core)


async def test_balances_set_account_balance_only(core: Any) -> None:
    await core.update_balances()
    assert _true_keys(core) == {"account_balance"}
    assert core.ready is False
    _assert_ready_derived(core)


async def test_start_sets_polling_key_only(core: Any) -> None:
    await core.start()
    try:
        assert _true_keys(core) == {"status_polling_initialized"}
        assert core.ready is False
        _assert_ready_derived(core)
    finally:
        await core.stop()


async def test_failed_trading_rules_keep_rule_keys_false(
    core: Any, gateway: KrakenGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fail(monkeypatch, gateway, "get_asset_pairs")
    with pytest.raises(httpx.ConnectError):
        await core.update_trading_rules()
    assert not (_true_keys(core) & _RULE_KEYS)
    assert core.ready is False
    _assert_ready_derived(core)


async def test_failed_balances_keep_account_balance_false(
    core: Any, gateway: KrakenGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fail(monkeypatch, gateway, "get_balances_ex")
    with pytest.raises(httpx.ConnectError):
        await core.update_balances()
    assert core.status_dict["account_balance"] is False
    assert core.ready is False
    _assert_ready_derived(core)


async def test_failed_refresh_after_success_keeps_key_true(
    core: Any, gateway: KrakenGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient refresh error does not un-initialise data that is still cached."""
    await core.update_balances()
    _fail(monkeypatch, gateway, "get_balances_ex")
    with pytest.raises(httpx.ConnectError):
        await core.update_balances()
    assert core.status_dict["account_balance"] is True


async def test_ready_only_when_all_subsystems_initialised(core: Any) -> None:
    await core.update_trading_rules()
    await core.update_balances()
    assert core.ready is False
    _assert_ready_derived(core)
    await core.start()
    try:
        assert all(core.status_dict.values())
        assert core.ready is True
        _assert_ready_derived(core)
    finally:
        await core.stop()


async def test_stop_resets_polling_key_and_ready(core: Any) -> None:
    await core.update_trading_rules()
    await core.update_balances()
    await core.start()
    assert core.ready is True
    await core.stop()
    status = core.status_dict
    assert status["status_polling_initialized"] is False
    assert status["trading_rule_initialized"] is True  # cached data survives stop()
    assert status["account_balance"] is True
    assert core.ready is False
    _assert_ready_derived(core)


async def test_ready_cannot_be_assigned_independently(core: Any) -> None:
    with pytest.raises(AttributeError):
        core.ready = True


async def test_status_dict_returns_fresh_dict(core: Any) -> None:
    first = core.status_dict
    first["account_balance"] = True
    first["bogus"] = True
    second = core.status_dict
    assert second is not first
    assert second["account_balance"] is False
    assert "bogus" not in second
    assert core.ready is False
