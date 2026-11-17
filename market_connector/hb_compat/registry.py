"""Registry of the exchanges that ship an hb_compat core (#265).

An explicit list, no entry points. Each exchange module is imported lazily, and only
when its name is requested, so an unknown lookup imports nothing.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from market_connector.hb_compat.core_protocol import ExchangeDescriptor

# (exchange name, descriptor module, attribute) per exchange.
_REGISTRATIONS: tuple[tuple[str, str, str], ...] = (
    ("kraken", "market_connector.exchanges.kraken.hb_compat.descriptor", "KRAKEN_DESCRIPTOR"),
)


def _load(module: str, attr: str) -> ExchangeDescriptor:
    descriptor: ExchangeDescriptor = getattr(importlib.import_module(module), attr)
    return descriptor


def list_exchanges() -> Sequence[ExchangeDescriptor]:
    """All registered exchange descriptors."""
    return tuple(_load(module, attr) for _, module, attr in _REGISTRATIONS)


def get_exchange(name: str) -> ExchangeDescriptor:
    """The descriptor named ``name``; ``KeyError`` if no such exchange is registered."""
    for registered, module, attr in _REGISTRATIONS:
        if registered == name:
            return _load(module, attr)
    raise KeyError(f"unknown exchange {name!r}")
