"""Coinbase Advanced Trade connector configuration with sandbox URL switching."""

from pydantic import BaseModel, ConfigDict, Field, computed_field


class CoinbaseConfig(BaseModel):
    """Immutable connector configuration; switches URLs based on sandbox flag."""

    model_config = ConfigDict(frozen=True)

    api_key: str
    secret_key: str
    sandbox: bool = False
    # seconds between REST polls of tracked legs (#249); must be positive
    leg_fill_poll_interval: float = Field(default=1.0, gt=0)

    # most non-terminal legs tracked for polling (#249); the oldest is dropped beyond this
    leg_fill_max_tracked: int = Field(default=4096, gt=0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def base_url(self) -> str:
        if self.sandbox:
            return "https://api-sandbox.coinbase.com/api/v3"
        return "https://api.coinbase.com/api/v3"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ws_url(self) -> str:
        if self.sandbox:
            return "wss://advanced-trade-ws-sandbox.coinbase.com"
        return "wss://advanced-trade-ws.coinbase.com"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ws_user_url(self) -> str:
        # Coinbase documents no sandbox host for the authenticated user WebSocket
        # (the sandbox is REST-only), so this is intentionally the production host
        # regardless of ``sandbox``. Unlike ``ws_url`` it must NOT be switched.
        return "wss://advanced-trade-ws-user.coinbase.com"
