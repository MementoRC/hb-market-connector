"""Mock transport clients for connector component testing (Tier 2).

Connector packages use these to test their mixins without real HTTP/WS.
"""

from __future__ import annotations

from typing import Any


class MockRestClient:
    """Mock REST client matching RestConnectorBase.request() interface.

    Keyed by endpoint name (same as RestConnectorBase), not method+path.

    Usage:
        mock = MockRestClient()
        mock.register("get_book", {"bids": [], "asks": []})
        result = await mock.request("get_book")
    """

    def __init__(self) -> None:
        self._responses: dict[str, Any] = {}

    def register(self, endpoint_name: str, response: dict[str, Any]) -> None:
        self._responses[endpoint_name] = response

    async def request(
        self,
        endpoint_name: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if endpoint_name not in self._responses:
            raise KeyError(f"No mock registered for endpoint '{endpoint_name}'")
        return self._responses[endpoint_name]


class MockWsClient:
    """Mock WebSocket client for testing subscription-based logic.

    Usage:
        mock = MockWsClient()
        mock.enqueue({"channel": "trades", "data": {...}})
        async for msg in mock:
            process(msg)
    """

    def __init__(self) -> None:
        self._messages: list[str] = []
        self._index = 0

    def enqueue(self, message: dict[str, Any]) -> None:
        import json

        self._messages.append(json.dumps(message))

    def __aiter__(self) -> MockWsClient:
        return self

    async def __anext__(self) -> str:
        if self._index >= len(self._messages):
            raise StopAsyncIteration
        msg = self._messages[self._index]
        self._index += 1
        return msg

    async def close(self) -> None:
        pass

    async def ping(self) -> None:
        pass
