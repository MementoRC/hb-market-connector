"""Connection specs for the IB Gateway transport."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IbConnectionSpec:
    """Parameters for connecting to a local IB Gateway instance.

    Defaults match a paper-trading IB Gateway (port 4002). For live trading,
    set port=4001 and paper=False.

    client_id must be a non-negative int, unique per concurrently-connected
    client (multiple clients on one Gateway need distinct ids). Use one stable
    id per bot. client_id 0 is the master client: it additionally receives
    orders placed manually in TWS, and only one client can be 0 at a time.

    fetch_all_open_orders (default True) calls reqAllOpenOrdersAsync on every
    connect and reconnect so orders from other API clients, from previous runs
    with another client_id, and (for the master client) TWS orders become
    visible, read-only (``OrderHandle.owned`` is False). IB does not keep
    other clients' orders live-updated after that snapshot.

    connect_timeout bounds the socket handshake and each startup sync request
    (per request, not in total); None or 0 disables it. connect_attempts is the
    total number of tries for the initial connect() when it fails with a
    timeout; other errors are raised at once.
    """

    host: str = "127.0.0.1"
    port: int = 4002  # paper IB Gateway; live = 4001
    client_id: int = 1
    account_id: str | None = None
    paper: bool = True
    market_data_type: int | None = None
    # 1=live, 2=frozen, 3=delayed, 4=delayed-frozen.
    # When None: defaults to 3 if paper else 1.
    readonly: bool = False  # read-only API session (no order placement)
    pace_api: bool = True  # send "+PACEAPI" so IB paces API messages for us
    reconnect_initial_delay: float = 1.0  # seconds before the first reconnect attempt
    reconnect_max_delay: float = 60.0  # backoff cap in seconds
    market_data_timeout: float = 5.0  # seconds to wait for first top-of-book / depth data
    what_if_timeout: float = 10.0  # seconds to wait for IB's what-if margin/commission answer
    pnl_timeout: float = 10.0  # seconds to wait for IB's first PnL snapshot
    depth_idle_release: float = 60.0  # seconds an unused depth stream stays open
    fetch_all_open_orders: bool = True  # reqAllOpenOrdersAsync on every (re)connect
    connect_timeout: float | None = 15.0  # seconds per handshake/sync request; None or 0 = none
    connect_attempts: int = 3  # total tries for the initial connect() on timeout

    def __post_init__(self) -> None:
        if self.connect_timeout is not None and self.connect_timeout < 0:
            raise ValueError(f"connect_timeout must be >= 0 or None; got {self.connect_timeout!r}")
        if self.connect_attempts < 1:
            raise ValueError(f"connect_attempts must be >= 1; got {self.connect_attempts!r}")
        if not isinstance(self.client_id, int) or isinstance(self.client_id, bool):
            raise TypeError(f"client_id must be an int; got {self.client_id!r}")
        if self.client_id < 0:
            raise ValueError(f"client_id must be >= 0; got {self.client_id!r}")
        if self.market_data_timeout <= 0:
            raise ValueError(f"market_data_timeout must be > 0; got {self.market_data_timeout!r}")
        if self.what_if_timeout <= 0:
            raise ValueError(f"what_if_timeout must be > 0; got {self.what_if_timeout!r}")
        if self.pnl_timeout <= 0:
            raise ValueError(f"pnl_timeout must be > 0; got {self.pnl_timeout!r}")
        if self.depth_idle_release <= 0:
            raise ValueError(f"depth_idle_release must be > 0; got {self.depth_idle_release!r}")
        if self.reconnect_initial_delay <= 0 or self.reconnect_max_delay <= 0:
            raise ValueError("reconnect delays must be > 0")
        if self.reconnect_initial_delay > self.reconnect_max_delay:
            raise ValueError(
                "reconnect_initial_delay must be <= reconnect_max_delay; "
                f"got {self.reconnect_initial_delay!r} > {self.reconnect_max_delay!r}"
            )
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
