"""Per-endpoint rate limit configuration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pydantic import BaseModel


@dataclass(frozen=True)
class Endpoint:
    """Declares a REST endpoint with its rate limit budget and optional typed response."""

    path: str
    method: str
    weight: int = 1
    limit: int = 10
    window: float = 1.0
    response_type: type[BaseModel] | None = None
