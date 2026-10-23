"""Structured instrument primitive for multi-asset exchanges.

InstrumentRef is a peer to ConnectorPair. Spot-only exchanges keep using
ConnectorPair (string-based); multi-asset exchanges accept InstrumentRef.
The two coexist within market_connector core.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import date
    from decimal import Decimal


class InstrumentType(StrEnum):
    STOCK = "STOCK"
    OPTION = "OPTION"
    FUTURE = "FUTURE"
    FOREX = "FOREX"
    CRYPTO = "CRYPTO"
    BOND = "BOND"
    CFD = "CFD"
    INDEX = "INDEX"


class OptionRight(StrEnum):
    CALL = "CALL"
    PUT = "PUT"


@dataclass(frozen=True)
class InstrumentRef:
    """Structured instrument identifier (peer to ConnectorPair).

    con_id, primary_exchange, local_symbol and contract_month are optional
    broker-side contract fields; unlike ``extras`` they take part in eq/hash.
    """

    symbol: str
    instrument_type: InstrumentType
    quote_currency: str | None = None
    exchange_hint: str | None = None
    expiry: date | None = None
    strike: Decimal | None = None
    option_right: OptionRight | None = None
    con_id: int | None = None
    primary_exchange: str | None = None
    local_symbol: str | None = None
    contract_month: str | None = None
    extras: Mapping[str, Any] = field(default_factory=dict, hash=False)
