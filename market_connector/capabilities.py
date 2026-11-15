"""Order capability descriptor: what order shapes a venue adapter can honour (#238)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from data_type_primitives import TriggerKind, TriggerReference
    from data_type_primitives.common import OrderType


class LinkedMode(Enum):
    """Linked-order structures a venue can place."""

    BRACKET = "bracket"
    OCO = "oco"


@dataclass(frozen=True)
class OrderCapabilities:
    """Immutable, hashable statement of the order features a gateway supports.

    ``order_types`` uses canonical ``data_type_primitives`` members. ``native_linked`` is
    True when the venue itself links bracket/OCO legs (as opposed to client-side emulation).
    """

    order_types: frozenset[OrderType]
    trigger_kinds: frozenset[TriggerKind]
    trigger_references: frozenset[TriggerReference]
    supports_trailing: bool
    linked: frozenset[LinkedMode]
    native_linked: bool
