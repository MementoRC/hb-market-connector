"""Typed REST response wrapper with lazy-cached parsing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Generic, TypeVar

import httpx
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class _Unset:
    """Sentinel marker for an un-parsed Response. Singleton via _UNSET."""

    __slots__ = ()


_UNSET: Final = _Unset()


@dataclass
class Response(Generic[T]):
    """Wraps a REST response, exposing raw payload, status, headers, and lazy parse().

    Construction-time invariant: if `_response_type` is set, `raw` must not be None.
    Mutation after construction is not re-validated — Response is a plain dataclass,
    not intended for mutation.
    """

    # `raw` excluded from repr to prevent flooding logs with large JSON payloads.
    # REQUIRED FIELD (no default) — do NOT add `default=None`; that would silently
    # bypass the __post_init__ invariant.
    raw: dict | list | None = field(repr=False)
    status_code: int = 200
    headers: httpx.Headers = field(default_factory=httpx.Headers)
    # Internal fields — excluded from repr / compare to keep the cache opaque
    # and prevent leaking endpoint identity into log spam.
    _endpoint: str = field(default="", repr=False, compare=False)
    _response_type: type[T] | None = field(default=None, repr=False, compare=False)
    _cached: T | dict | list | None | _Unset = field(
        default=_UNSET,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        # Invariant enforced at CONSTRUCTION TIME ONLY.
        if self.raw is None and self._response_type is not None:
            raise ValueError(
                f"{self._endpoint}: empty body but response_type="
                f"{self._response_type.__name__} declared. "
                f"Use response_type=None for 204/no-body endpoints, or a "
                f"RootModel/None-aware schema."
            )
