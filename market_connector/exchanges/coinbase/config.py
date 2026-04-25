"""Coinbase Advanced Trade connector configuration with sandbox URL switching."""

from pydantic import BaseModel, ConfigDict, computed_field


class CoinbaseConfig(BaseModel):
    """Immutable connector configuration; switches URLs based on sandbox flag."""

    model_config = ConfigDict(frozen=True)

    api_key: str
    secret_key: str
    sandbox: bool = False

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
