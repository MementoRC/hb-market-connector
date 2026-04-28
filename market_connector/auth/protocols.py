"""Signer Protocol and Request envelope (spec §6.1).

Defines the structural interface consumed by DeclarativeRestSigner (Task 4)
and the REST transport (Task 12).  Nothing here depends on any concrete
signing implementation — import cost is a single stdlib import.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Request:
    """Immutable HTTP request envelope passed through the signing pipeline.

    Fields (exactly 6, per spec §6.1):
        method:    HTTP verb, e.g. "GET", "POST".
        url:       Fully-qualified URL including scheme and host.
        path:      URL path component only, e.g. "/v1/orders".
        headers:   Mutable header mapping (caller owns); signing may return
                   a new Request with an updated mapping.
        body:      Request body as str, bytes, or None.
        qs_params: Query-string parameters as a string-keyed mapping.
    """

    method: str
    url: str
    path: str
    headers: dict
    body: str | bytes | None
    qs_params: dict


@runtime_checkable
class Signer(Protocol):
    """Structural interface for any component that can sign a Request.

    A class satisfies this Protocol if it has an async ``sign`` method that
    accepts a ``Request`` and returns a ``Request``.  Use
    ``isinstance(obj, Signer)`` for runtime checks (enabled by
    ``@runtime_checkable``).
    """

    async def sign(self, request: Request) -> Request: ...
