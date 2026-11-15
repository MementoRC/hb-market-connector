"""cancel_linked on SupportsBracket / SupportsOco: protocol shape and contract (#251).

RED acceptance tests (goal-first TDD). Target API:

    SupportsBracket.cancel_linked(handle: BracketHandle) -> LinkedCancelOutcome   # async
    SupportsOco.cancel_linked(handle: OcoHandle) -> LinkedCancelOutcome           # async

``LinkedCancelOutcome`` / ``CancelOutcome`` are the slim dtp v0.12.0 types (no GroupState,
``group_id: str`` only). ``cancel_linked`` is NOT added to ``ExecutionGateway`` and Coinbase
keeps implementing neither Protocol. IB behaviour lives in
tests/exchanges/interactive_brokers/test_cancel_linked_251.py.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from market_connector import protocols

_PROTOS = [("SupportsBracket", "BracketHandle"), ("SupportsOco", "OcoHandle")]


class _BracketerNoCancel:
    async def place_bracket(self, target: Any, spec: Any, *, options: Any = None) -> Any:
        raise NotImplementedError


class _OcoerNoCancel:
    async def place_oco(self, target: Any, spec: Any, *, options: Any = None) -> Any:
        raise NotImplementedError


class _BracketerWithCancel(_BracketerNoCancel):
    async def cancel_linked(self, handle: Any) -> Any:
        raise NotImplementedError


class _OcoerWithCancel(_OcoerNoCancel):
    async def cancel_linked(self, handle: Any) -> Any:
        raise NotImplementedError


class TestProtocolDeclaration:
    @pytest.mark.parametrize(("proto", "_handle"), _PROTOS)
    def test_cancel_linked_is_a_coroutine_function(self, proto: str, _handle: str) -> None:
        method = getattr(getattr(protocols, proto), "cancel_linked")  # noqa: B009
        assert inspect.iscoroutinefunction(method)

    @pytest.mark.parametrize(("proto", "_handle"), _PROTOS)
    def test_signature_is_self_handle(self, proto: str, _handle: str) -> None:
        method = getattr(getattr(protocols, proto), "cancel_linked")  # noqa: B009
        params = list(inspect.signature(method).parameters.values())
        assert [p.name for p in params] == ["self", "handle"]
        assert params[1].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD

    @pytest.mark.parametrize(("proto", "handle"), _PROTOS)
    def test_handle_parameter_is_annotated_with_its_handle_protocol(
        self, proto: str, handle: str
    ) -> None:
        method = getattr(getattr(protocols, proto), "cancel_linked")  # noqa: B009
        annotation = inspect.signature(method).parameters["handle"].annotation
        assert handle in str(annotation)

    @pytest.mark.parametrize(("proto", "_handle"), _PROTOS)
    def test_returns_dtp_linked_cancel_outcome(self, proto: str, _handle: str) -> None:
        method = getattr(getattr(protocols, proto), "cancel_linked")  # noqa: B009
        assert "LinkedCancelOutcome" in str(inspect.signature(method).return_annotation)

    def test_not_added_to_execution_gateway(self) -> None:
        assert "cancel_linked" not in dir(protocols.ExecutionGateway)


class TestStructuralConformance:
    def test_bracket_implementer_must_now_have_cancel_linked(self) -> None:
        assert not isinstance(_BracketerNoCancel(), protocols.SupportsBracket)
        assert isinstance(_BracketerWithCancel(), protocols.SupportsBracket)

    def test_oco_implementer_must_now_have_cancel_linked(self) -> None:
        assert not isinstance(_OcoerNoCancel(), protocols.SupportsOco)
        assert isinstance(_OcoerWithCancel(), protocols.SupportsOco)

    def test_cancel_linked_alone_does_not_satisfy_either_protocol(self) -> None:
        class _OnlyCancel:
            async def cancel_linked(self, handle: Any) -> Any:
                raise NotImplementedError

        assert not isinstance(_OnlyCancel(), protocols.SupportsBracket)
        assert not isinstance(_OnlyCancel(), protocols.SupportsOco)

    def test_bracket_only_does_not_satisfy_oco(self) -> None:
        assert not isinstance(_BracketerWithCancel(), protocols.SupportsOco)
        assert not isinstance(_OcoerWithCancel(), protocols.SupportsBracket)


class TestCoinbaseUnaffected:
    def test_coinbase_implements_neither_protocol_nor_cancel_linked(self) -> None:
        from market_connector.exchanges.coinbase.coinbase_gateway import CoinbaseGateway
        from market_connector.exchanges.coinbase.config import CoinbaseConfig
        from market_connector.testing import contract

        gw = CoinbaseGateway(
            CoinbaseConfig(api_key="k", secret_key="raw_hmac_secret", sandbox=True)
        )
        assert not isinstance(gw, protocols.SupportsBracket)
        assert not isinstance(gw, protocols.SupportsOco)
        assert not hasattr(gw, "cancel_linked")
        assert contract.check_linked_protocol_capabilities(gw) == []
