"""Connection specs for the IB Gateway transport."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IbConnectionSpec:
    """Parameters for connecting to a local IB Gateway instance.

    Defaults match a paper-trading IB Gateway (port 4002). For live trading,
    set port=4001 and paper=False. The client_id should be unique per
    concurrently-connected client (multiple clients on one Gateway need
    distinct ids).
    """

    host: str = "127.0.0.1"
    port: int = 4002  # paper IB Gateway; live = 4001
    client_id: int = 1
    account_id: str | None = None
    paper: bool = True
    market_data_type: int | None = None
    # 1=live, 2=frozen, 3=delayed, 4=delayed-frozen.
    # When None: defaults to 3 if paper else 1.

    def __post_init__(self) -> None:
        if self.market_data_type is not None and self.market_data_type not in (1, 2, 3, 4):
            raise ValueError(
                f"market_data_type must be 1, 2, 3, or 4; got {self.market_data_type!r}"
            )

    @property
    def resolved_market_data_type(self) -> int:
        """Return effective IB market data type: explicit value, or 3/1 by paper flag."""
        if self.market_data_type is not None:
            return self.market_data_type
        return 3 if self.paper else 1
