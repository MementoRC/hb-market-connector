"""Canonical logger factory backed by hb-logger (ADR 0001 Group D).

Wraps :mod:`logger` (hb-logger) so market_connector call sites obtain
loggers through a single sanctioned entry point instead of calling
``logging.getLogger`` directly. Importing :mod:`logger` registers
``HummingbotLogger`` as the active logger class via its module-level
``logging.setLoggerClass`` call, so every logger returned by
:func:`get_logger` exposes the richer ``notify()``/``network()`` interface
in addition to the standard ``logging.Logger`` API. See ADR 0001 Group D
Target 1 (event-bus) for the precedent this follows.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

import logger as _hb_logger  # noqa: F401 — side effect: registers HummingbotLogger as the active logging.Logger class

if TYPE_CHECKING:
    from logger import HummingbotLogger

__all__ = ["get_logger"]


def get_logger(name: str) -> HummingbotLogger:
    """Return a :class:`HummingbotLogger` for ``name``.

    Importing this module (which imports :mod:`logger` for its side effect)
    ensures ``logging.setLoggerClass(HummingbotLogger)`` has already run, so
    the stdlib ``logging.getLogger`` call below returns a
    ``HummingbotLogger`` instance rather than a bare ``logging.Logger``.
    """
    return cast("HummingbotLogger", logging.getLogger(name))
