"""RED acceptance tests for #260: Kraken SPOT must reject ``sandbox=True``.

Kraken spot has no sandbox. ``sandbox=True`` used to point REST at the Kraken
Futures demo host while the WS stayed on production (a mixed environment), so
every spot construction path must now raise ``ValueError`` (mentioning
"sandbox" and "spot") at construction, before any transport or URL is built.

Construction paths covered:
    - ``KrakenConfig``            (kraken_gateway.py)
    - ``KrakenGateway``           (kraken_gateway.py)
    - ``KrakenConnectorBridge``   (hb_compat/kraken_bridge.py)
    - ``KrakenConnectorCore``     (hb_compat/kraken_connector_core.py) takes a built
      gateway and has no sandbox parameter, so it is covered through the gateway.

No Kraken futures connector exists in this tree, so there is no futures
regression guard.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from market_connector.exchanges.kraken import kraken_gateway as gateway_module
from market_connector.exchanges.kraken.hb_compat.kraken_bridge import KrakenConnectorBridge
from market_connector.exchanges.kraken.kraken_gateway import KrakenConfig, KrakenGateway
from tests.exchanges.kraken.conftest import _TEST_API_KEY, _TEST_SECRET_B64

PROD_REST = "https://api.kraken.com"
PROD_WS = "wss://ws.kraken.com"
PROD_WS_AUTH = "wss://ws-auth.kraken.com"


def _build_config(**kw):
    return KrakenConfig(api_key=_TEST_API_KEY, secret_key=_TEST_SECRET_B64, **kw)


def _build_gateway(**kw):
    return KrakenGateway(api_key=_TEST_API_KEY, secret_key=_TEST_SECRET_B64, **kw)


def _build_bridge(**kw):
    return KrakenConnectorBridge(api_key=_TEST_API_KEY, secret_key=_TEST_SECRET_B64, **kw)


SPOT_BUILDERS = [
    pytest.param(_build_config, id="KrakenConfig"),
    pytest.param(_build_gateway, id="KrakenGateway"),
    pytest.param(_build_bridge, id="KrakenConnectorBridge"),
]


@pytest.fixture
def io_spies(monkeypatch):
    """Replace the REST transport and WS connector with recorders (no real I/O)."""
    transport = MagicMock(name="KrakenTransport")
    ws = MagicMock(name="WsConnectorBase")
    monkeypatch.setattr(gateway_module, "KrakenTransport", transport)
    monkeypatch.setattr(gateway_module, "WsConnectorBase", ws)
    return transport, ws


# ---------------------------------------------------------------------------
# 1. sandbox=True is rejected on every spot entry point
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("build", SPOT_BUILDERS)
def test_sandbox_true_raises_value_error_mentioning_sandbox_and_spot(build, io_spies):
    with pytest.raises(ValueError) as excinfo:
        build(sandbox=True)
    message = str(excinfo.value).lower()
    assert "sandbox" in message
    assert "spot" in message


# ---------------------------------------------------------------------------
# 2. Rejection happens before any transport / URL resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "build",
    [_build_gateway, _build_bridge],
    ids=["KrakenGateway", "KrakenConnectorBridge"],
)
def test_sandbox_true_builds_no_rest_or_ws_transport(build, io_spies):
    transport, ws = io_spies
    with pytest.raises(ValueError):
        build(sandbox=True)
    transport.assert_not_called()
    ws.assert_not_called()


def test_sandbox_true_config_resolves_no_urls():
    with pytest.raises(ValueError):
        _build_config(sandbox=True)


def test_sandbox_true_config_rejected_even_with_explicit_base_url():
    """An explicit URL override must not bypass the spot sandbox rejection."""
    with pytest.raises(ValueError):
        _build_config(sandbox=True, base_url="https://example.invalid")


# ---------------------------------------------------------------------------
# 3. sandbox=False / default still builds with production spot URLs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kwargs", [{}, {"sandbox": False}], ids=["default", "explicit_false"])
def test_config_uses_production_urls(kwargs):
    cfg = _build_config(**kwargs)
    assert cfg.sandbox is False
    assert cfg.base_url == PROD_REST
    assert cfg.ws_url == PROD_WS
    assert cfg.ws_auth_url == PROD_WS_AUTH


@pytest.mark.parametrize("kwargs", [{}, {"sandbox": False}], ids=["default", "explicit_false"])
def test_gateway_uses_production_urls(kwargs, io_spies):
    transport, ws = io_spies
    gw = _build_gateway(**kwargs)
    assert gw._config.base_url == PROD_REST
    assert transport.call_args.kwargs["base_url"] == PROD_REST
    assert ws.call_args_list[0].kwargs["url"] == PROD_WS  # #274: 2nd call is the private WS


@pytest.mark.parametrize("kwargs", [{}, {"sandbox": False}], ids=["default", "explicit_false"])
def test_bridge_uses_production_urls(kwargs, io_spies):
    transport, ws = io_spies
    bridge = _build_bridge(**kwargs)
    assert bridge.gateway._config.base_url == PROD_REST
    assert transport.call_args.kwargs["base_url"] == PROD_REST
    assert ws.call_args_list[0].kwargs["url"] == PROD_WS  # #274: 2nd call is the private WS
