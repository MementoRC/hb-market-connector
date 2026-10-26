"""Kraken order-age matching penalties (stub; implemented under #116)."""

from __future__ import annotations


def cancel_penalty(age_s: float) -> int:
    """Matching-pool units a CancelOrder costs for an order ``age_s`` seconds old."""
    raise NotImplementedError


def amend_penalty(age_s: float) -> int:
    """Matching-pool units an AmendOrder costs (fixed 1 plus age penalty)."""
    raise NotImplementedError


def edit_penalty(age_s: float) -> int:
    """Matching-pool units an EditOrder costs (fixed 1 plus age penalty)."""
    raise NotImplementedError
