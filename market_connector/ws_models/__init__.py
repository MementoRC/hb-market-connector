"""WS models — message decoder and auth lifecycle hooks."""

from market_connector.ws_models.auth_models import (
    ListenKeyAuth,
    PassThroughAuth,
    PerMessageSignAuth,
    RestClient,
    SendCallable,
    SignedLoginMessageAuth,
    TokenFetchAuth,
    TokenInjectStrategy,
    WsAuthModel,
    build_ws_auth,
)
from market_connector.ws_models.decoder import (
    JsonEnvelopeDecoder,
    NormalizedWsMessage,
    PositionalArrayDecoder,
    WsMessageKind,
    WsShapeDecoder,
)

__all__ = [
    # decoder
    "WsMessageKind",
    "NormalizedWsMessage",
    "WsShapeDecoder",
    "JsonEnvelopeDecoder",
    "PositionalArrayDecoder",
    # auth models
    "WsAuthModel",
    "SendCallable",
    "TokenInjectStrategy",
    "RestClient",
    "SignedLoginMessageAuth",
    "PerMessageSignAuth",
    "TokenFetchAuth",
    "ListenKeyAuth",
    "PassThroughAuth",
    "build_ws_auth",
]
