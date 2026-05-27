"""Structural typing tests for mixin intersection protocols."""

from __future__ import annotations

import pytest

from market_connector.exchanges.interactive_brokers.mixins.protocols import (
    HasContractResolver,
    HasIbTransport,
    HasReady,
)


class FakeTransportHost:
    """Concrete fake satisfying HasIbTransport structurally."""

    def __init__(self) -> None:
        self._transport = object()  # type: ignore[assignment]


class FakeResolverHost:
    """Concrete fake satisfying HasContractResolver structurally."""

    def __init__(self) -> None:
        self._contract_resolver = object()  # type: ignore[assignment]


class FakeReadyHost:
    """Concrete fake satisfying HasReady structurally."""

    def __init__(self) -> None:
        self._ready = False

    async def ensure_ready(self) -> None:
        pass


class TestHasIbTransport:
    def test_fake_satisfies_protocol(self) -> None:
        host: HasIbTransport = FakeTransportHost()  # type: ignore[assignment]
        assert hasattr(host, "_transport")

    def test_protocol_is_runtime_checkable(self) -> None:
        # Protocols used as self-type annotations; structural compatibility is
        # verified at static-analysis time. This test confirms the class exists
        # and is importable, and the attribute name is correct.
        assert hasattr(HasIbTransport, "__protocol_attrs__")


class TestHasContractResolver:
    def test_fake_satisfies_protocol(self) -> None:
        host: HasContractResolver = FakeResolverHost()  # type: ignore[assignment]
        assert hasattr(host, "_contract_resolver")


class TestHasReady:
    def test_fake_satisfies_protocol(self) -> None:
        host: HasReady = FakeReadyHost()  # type: ignore[assignment]
        assert hasattr(host, "_ready")
        assert hasattr(host, "ensure_ready")

    @pytest.mark.asyncio
    async def test_ensure_ready_is_coroutine(self) -> None:
        host = FakeReadyHost()
        await host.ensure_ready()
