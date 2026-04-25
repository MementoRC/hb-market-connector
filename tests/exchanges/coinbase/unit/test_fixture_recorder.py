"""Unit tests for fixture_recorder — all HTTP calls are mocked."""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from market_connector.exchanges.coinbase.tools.fixture_recorder import capture_rest, main, sanitize

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# sanitize() tests
# ---------------------------------------------------------------------------


def test_sanitize_redacts_sensitive_keys() -> None:
    obj: dict[str, Any] = {
        "api_key": "real-key",
        "secret": "real-secret",
        "email": "user@example.com",
        "user_id": "uid-123",
        "price": "42.00",
    }
    result = sanitize(obj)
    assert result["api_key"] == "REDACTED_API_KEY"
    assert result["secret"] == "REDACTED_SECRET"
    assert result["email"] == "REDACTED_EMAIL"
    assert result["user_id"] == "REDACTED_USER_ID"
    assert result["price"] == "42.00"  # non-sensitive key is preserved


def test_sanitize_replaces_uuids_in_strings() -> None:
    obj = "order-12345678-1234-1234-1234-123456789abc-done"
    result = sanitize(obj)
    assert "12345678-1234-1234-1234-123456789abc" not in result
    assert "00000000-0000-0000-0000-000000000001" in result


def test_sanitize_recurses_into_nested_dict() -> None:
    obj: dict[str, Any] = {"outer": {"api_key": "key", "value": 1}}
    result = sanitize(obj)
    assert result["outer"]["api_key"] == "REDACTED_API_KEY"
    assert result["outer"]["value"] == 1


def test_sanitize_recurses_into_list() -> None:
    obj: list[Any] = [{"api_key": "k"}, {"price": "1.0"}]
    result = sanitize(obj)
    assert result[0]["api_key"] == "REDACTED_API_KEY"
    assert result[1]["price"] == "1.0"


def test_sanitize_passthrough_for_non_sensitive_scalars() -> None:
    assert sanitize(42) == 42
    assert sanitize(3.14) == 3.14
    assert sanitize(True) is True  # noqa: FBT003
    assert sanitize(None) is None


def test_sanitize_string_without_uuid_unchanged() -> None:
    assert sanitize("BTC-USD") == "BTC-USD"


# ---------------------------------------------------------------------------
# capture_rest() tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_gateway() -> MagicMock:
    gw = MagicMock()
    gw._rest = MagicMock()
    return gw


async def test_capture_rest_writes_sanitized_json(tmp_path: Path, mock_gateway: MagicMock) -> None:
    mock_gateway._rest.request = AsyncMock(
        return_value={"iso": "2024-01-01T00:00:00Z", "api_key": "should-be-redacted"}
    )

    await capture_rest(mock_gateway, "server_time", tmp_path)

    output_file = tmp_path / "server_time.json"
    assert output_file.exists()
    data = json.loads(output_file.read_text())
    assert data["api_key"] == "REDACTED_API_KEY"
    assert data["iso"] == "2024-01-01T00:00:00Z"


async def test_capture_rest_uses_correct_params_for_product_book(
    tmp_path: Path, mock_gateway: MagicMock
) -> None:
    mock_gateway._rest.request = AsyncMock(return_value={"bids": [], "asks": []})

    await capture_rest(mock_gateway, "product_book", tmp_path)

    mock_gateway._rest.request.assert_called_once_with(
        "product_book", params={"product_id": "BTC-USD"}
    )


async def test_capture_rest_uses_correct_params_for_candles(
    tmp_path: Path, mock_gateway: MagicMock
) -> None:
    mock_gateway._rest.request = AsyncMock(return_value={"candles": []})

    await capture_rest(mock_gateway, "candles", tmp_path)

    mock_gateway._rest.request.assert_called_once_with(
        "candles", params={"product_id": "BTC-USD", "granularity": "ONE_HOUR"}
    )


async def test_capture_rest_uses_empty_params_for_unknown_endpoint(
    tmp_path: Path, mock_gateway: MagicMock
) -> None:
    mock_gateway._rest.request = AsyncMock(return_value={"data": "x"})

    await capture_rest(mock_gateway, "unknown_endpoint", tmp_path)

    mock_gateway._rest.request.assert_called_once_with("unknown_endpoint", params={})


async def test_capture_rest_handles_exception_gracefully(
    tmp_path: Path, mock_gateway: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    mock_gateway._rest.request = AsyncMock(side_effect=RuntimeError("network error"))

    # Should not raise
    await capture_rest(mock_gateway, "server_time", tmp_path)

    captured = capsys.readouterr()
    assert "Failed" in captured.out
    assert "server_time" in captured.out
    assert not (tmp_path / "server_time.json").exists()


async def test_capture_rest_creates_output_file_named_by_endpoint(
    tmp_path: Path, mock_gateway: MagicMock
) -> None:
    mock_gateway._rest.request = AsyncMock(return_value={"accounts": []})

    await capture_rest(mock_gateway, "accounts", tmp_path)

    assert (tmp_path / "accounts.json").exists()


# ---------------------------------------------------------------------------
# main() integration test (mocked gateway lifecycle)
# ---------------------------------------------------------------------------


async def test_main_creates_rest_dir_and_captures_endpoints(tmp_path: Path) -> None:
    """Verify main() creates the output/rest/ dir and calls capture_rest per endpoint."""
    fake_response: dict[str, Any] = {"iso": "2024-01-01T00:00:00Z"}

    with (
        patch(
            "market_connector.exchanges.coinbase.tools.fixture_recorder.CoinbaseGateway"
        ) as mock_gw_cls,
        patch(
            "market_connector.exchanges.coinbase.tools.fixture_recorder.CoinbaseConfig"
        ) as mock_cfg_cls,
        patch(
            "market_connector.exchanges.coinbase.tools.fixture_recorder.capture_rest",
            new_callable=AsyncMock,
        ) as mock_capture,
    ):
        gw_instance = MagicMock()
        gw_instance.start = AsyncMock()
        gw_instance.stop = AsyncMock()
        gw_instance._rest.request = AsyncMock(return_value=fake_response)
        mock_gw_cls.return_value = gw_instance
        mock_cfg_cls.return_value = MagicMock()

        test_args = [
            "fixture_recorder",
            "--api-key",
            "test-key",
            "--secret",
            "test-secret",
            "--output",
            str(tmp_path),
            "--endpoints",
            "server_time,accounts",
        ]
        with patch.object(sys, "argv", test_args):
            await main()

        gw_instance.start.assert_called_once()
        gw_instance.stop.assert_called_once()
        assert mock_capture.call_count == 2
