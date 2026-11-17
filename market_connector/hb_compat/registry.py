"""Registry of the exchanges that ship an hb_compat core (#265).

An explicit list, no entry points. Each exchange module is imported lazily so listing
or looking up one exchange does not import the others.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from market_connector.hb_compat.core_protocol import ExchangeDescriptor

# (descriptor module, attribute) per exchange.
_REGISTRATIONS: tuple[tuple[str, str], ...] = (
    ("market_connector.exchanges.kraken.hb_compat.descriptor", "KRAKEN_DESCRIPTOR"),
)


def list_exchanges() -> Sequence[ExchangeDescriptor]:
    """All registered exchange descriptors."""
    return tuple(getattr(importlib.import_module(module), attr) for module, attr in _REGISTRATIONS)


def get_exchange(name: str) -> ExchangeDescriptor:
    """The descriptor named ``name``; ``KeyError`` if no such exchange is registered."""
    for descriptor in list_exchanges():
        if descriptor.name == name:
            return descriptor
    raise KeyError(f"unknown exchange {name!r}")
