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
