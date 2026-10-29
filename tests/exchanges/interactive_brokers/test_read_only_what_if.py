"""what_if must fail fast under a read-only connection (follow-up to #151).

IB treats whatIf as an order-API request, so on a READ_ONLY_API Gateway it hangs until
``what_if_timeout``. In readonly mode the transport raises IbReadOnlyModeError instead.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec

from .test_connection_lifecycle import _make

_TEST_DIR = Path(__file__).parent


class TestReadOnlyWhatIf:
    async def test_what_if_raises_without_calling_ib(self) -> None:
        from market_connector.exchanges.interactive_brokers.exceptions import (  # noqa: PLC0415
            IbReadOnlyModeError,
        )
        from market_connector.orders import HBOrder, OrderType, TradeType  # noqa: PLC0415

        transport, ib, _ = _make(IbConnectionSpec(readonly=True))
        await transport.connect()
        ib.whatIfOrderAsync = AsyncMock()  # type: ignore[attr-defined]
        order = HBOrder(
            order_type=OrderType.MARKET, side=TradeType.BUY, amount=Decimal(1), price=None
        )
        with pytest.raises(IbReadOnlyModeError):
            await transport.what_if(MagicMock(), order)
        ib.whatIfOrderAsync.assert_not_called()


def _calls_what_if(func: ast.AsyncFunctionDef | ast.FunctionDef) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "what_if"
        for node in ast.walk(func)
    )


def _references_live_readonly(func: ast.AsyncFunctionDef | ast.FunctionDef) -> bool:
    return any(isinstance(n, ast.Name) and n.id == "live_readonly" for n in ast.walk(func))


class TestLiveWhatIfTestsSkipUnderReadOnly:
    def test_every_live_what_if_test_references_live_readonly(self) -> None:
        offenders: list[str] = []
        for path in sorted(_TEST_DIR.glob("test_integration*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
                    and node.name.startswith("test_")
                    and _calls_what_if(node)
                    and not _references_live_readonly(node)
                ):
                    offenders.append(f"{path.name}::{node.name}")
        assert not offenders, f"what-if live tests must skip under IB_READONLY: {offenders}"
