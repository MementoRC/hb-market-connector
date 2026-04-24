"""Per-endpoint rate limit configuration."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Endpoint:
    """Declares a REST endpoint with its rate limit budget."""

    path: str
    method: str
    weight: int = 1
    limit: int = 10
    window: float = 1.0
