"""PoolSpec — declarative pool descriptor for rate limiters."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PoolSpec:
    """Declarative descriptor for a single token-bucket pool.

    Args:
        name: Human-readable pool identifier.
        capacity: Maximum tokens in the bucket (must be > 0).
        refill_rate: Tokens added per second (must be > 0).
    """

    name: str
    capacity: int
    refill_rate: float

    def __post_init__(self) -> None:
        if self.capacity <= 0:
            raise ValueError(f"capacity must be > 0, got {self.capacity!r}")
        if self.refill_rate <= 0:
            raise ValueError(f"refill_rate must be > 0, got {self.refill_rate!r}")
