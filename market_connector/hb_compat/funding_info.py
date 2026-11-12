"""Legacy hummingbot ``FundingInfo`` / ``FundingInfoUpdate`` (#192).

Re-exports the canonical mutable ``FundingInfo`` and ``FundingInfoUpdate`` from
``hb-data-type-primitives`` instead of keeping a local duplicate. Also provides
module-level converters between that canonical ``FundingInfo`` and the frozen
pydantic domain model ``market_connector.primitives.FundingInfo``.
"""

from data_type_primitives import FundingInfo, FundingInfoUpdate

from market_connector.primitives import FundingInfo as FundingInfoPrimitive

__all__ = [
    "FundingInfo",
    "FundingInfoUpdate",
    "funding_info_from_primitive",
    "funding_info_to_primitive",
]


def funding_info_to_primitive(fi: FundingInfo) -> FundingInfoPrimitive:
    """Convert a canonical ``FundingInfo`` to the frozen domain model."""
    return FundingInfoPrimitive(
        trading_pair=fi.trading_pair,
        index_price=fi.index_price,
        mark_price=fi.mark_price,
        next_funding_utc_timestamp=fi.next_funding_utc_timestamp,
        rate=fi.rate,
    )


def funding_info_from_primitive(p: FundingInfoPrimitive) -> FundingInfo:
    """Build a canonical ``FundingInfo`` from the frozen domain model."""
    return FundingInfo(
        p.trading_pair,
        p.index_price,
        p.mark_price,
        p.next_funding_utc_timestamp,
        p.rate,
    )
