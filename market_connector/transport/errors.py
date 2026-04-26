"""Transport-layer error types for typed response parsing."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from pydantic import ValidationError

from market_connector.exceptions import GatewayError

_MAX_ERRORS_IN_MSG: Final = 3


class MarketConnectorParseError(GatewayError):
    """Raised when a transport response fails Pydantic validation.

    Carries the registry endpoint name (stable across path migrations), the
    raw payload, and the original ValidationError. The original is also
    chained via __cause__ when raised with `from e`.
    """

    def __init__(self, endpoint: str, raw: Any, original: ValidationError):
        # `raw` is typed `Any` (not `dict | list | None` like `Response.raw`)
        # so this error type can also be raised from non-Response code paths
        # (e.g. ad-hoc payload validation in mixins). Intentional widening.
        self.endpoint = endpoint
        self.raw = raw
        self.original = original
        errs = original.errors()
        if errs:
            head = "; ".join(
                f"{'.'.join(str(x) for x in e['loc'])}[{e['type']}]: {e['msg']}"
                for e in errs[:_MAX_ERRORS_IN_MSG]
            )
            suffix = (
                f" (+{len(errs) - _MAX_ERRORS_IN_MSG} more)"
                if len(errs) > _MAX_ERRORS_IN_MSG
                else ""
            )
            super().__init__(f"{endpoint}: {len(errs)} validation error(s); {head}{suffix}")
        else:
            # Pydantic guarantees ValidationError carries >=1 error — defensive only.
            super().__init__(f"{endpoint}: validation error (no details from pydantic)")

    def errors(self) -> list[dict]:
        """Convenience pass-through to the underlying pydantic error list."""
        return self.original.errors()
