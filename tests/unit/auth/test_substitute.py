import pytest

from market_connector.auth.substitute import InvalidTemplateVariable, Surface, substitute


def test_substitute_simple_variable():
    result = substitute("{api_key}", {"api_key": "abc123"}, surface=Surface.REST)
    assert result == "abc123"


def test_substitute_multi_variable():
    result = substitute(
        "{ts}{method}{path}", {"ts": "1234", "method": "GET", "path": "/v3"}, surface=Surface.REST
    )
    assert result == "1234GET/v3"


def test_substitute_unknown_variable_raises_at_construction():
    with pytest.raises(InvalidTemplateVariable, match="unknown variable"):
        substitute("{not_a_real_var}", {}, surface=Surface.REST)


def test_substitute_rest_only_var_in_ws_surface_raises():
    with pytest.raises(InvalidTemplateVariable, match="not valid in WS"):
        substitute("{method}", {"method": "GET"}, surface=Surface.WS)


def test_substitute_path_bytes_returns_bytes():
    result = substitute(
        "{path_bytes}", {"path": "/0/private/AddOrder"}, surface=Surface.REST, as_bytes=True
    )
    assert result == b"/0/private/AddOrder"


def test_substitute_secret_only_in_sig_input():
    # {secret} valid in SIG_INPUT surface, not in OUTPUT
    with pytest.raises(InvalidTemplateVariable, match="not valid in OUTPUT"):
        substitute("{secret}", {"secret": "x"}, surface=Surface.OUTPUT)
