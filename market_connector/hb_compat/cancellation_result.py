"""Legacy hummingbot ``CancellationResult`` (hummingbot.core.data_type.cancellation_result).

A ``NamedTuple`` with the same positional shape as hummingbot's. ``to_primitive()`` /
``from_primitive()`` convert to and from the frozen canonical
``market_connector.primitives.CancellationResult``.
"""

from typing import NamedTuple, Self

from market_connector.primitives import CancellationResult as CancellationResultPrimitive


class CancellationResult(NamedTuple):
    """Outcome of a single order cancellation (hummingbot ``CancellationResult``)."""

    order_id: str
    success: bool

    def to_primitive(self) -> CancellationResultPrimitive:
        """Convert to the frozen canonical ``CancellationResult``."""
        return CancellationResultPrimitive(order_id=self.order_id, success=self.success)

    @classmethod
    def from_primitive(cls, primitive: CancellationResultPrimitive) -> Self:
        """Build a legacy ``CancellationResult`` from the frozen canonical model."""
        return cls(primitive.order_id, primitive.success)
