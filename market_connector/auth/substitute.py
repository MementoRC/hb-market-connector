"""Substitution-variable engine for auth template expansion.

Provides a finite, documented set of 19 template variables (§6.6) with
surface-scope validation. Unknown variables and wrong-surface usage raise
InvalidTemplateVariable at call time (spec: "at spec construction time").
"""

from __future__ import annotations

import string
from dataclasses import dataclass
from enum import Enum


class Surface(Enum):
    REST = "REST"
    WS = "WS"
    SIG_INPUT = "SIG_INPUT"
    OUTPUT = "OUTPUT"


class InvalidTemplateVariable(ValueError):  # noqa: N818
    """Raised when a template variable is unknown or used in the wrong surface."""


@dataclass(frozen=True)
class _VarDef:
    valid_surfaces: frozenset[Surface]


# 19 variables from spec §6.6 with explicit surface scopes
_VARIABLE_DEFINITIONS: dict[str, _VarDef] = {
    # REST + WS
    "api_key": _VarDef(frozenset({Surface.REST, Surface.WS})),
    "ts": _VarDef(frozenset({Surface.REST, Surface.WS})),
    "nonce": _VarDef(frozenset({Surface.REST, Surface.WS})),
    "rand_hex": _VarDef(frozenset({Surface.REST, Surface.WS})),
    # REST only
    "method": _VarDef(frozenset({Surface.REST})),
    "path": _VarDef(frozenset({Surface.REST})),
    "path_bytes": _VarDef(frozenset({Surface.REST})),
    "body": _VarDef(frozenset({Surface.REST})),
    "qs": _VarDef(frozenset({Surface.REST})),
    "qs_sorted": _VarDef(frozenset({Surface.REST})),
    "recv_window": _VarDef(frozenset({Surface.REST})),
    "memo": _VarDef(frozenset({Surface.REST})),
    "passphrase": _VarDef(frozenset({Surface.REST})),
    "host": _VarDef(frozenset({Surface.REST})),
    # SIG_INPUT only
    "secret": _VarDef(frozenset({Surface.SIG_INPUT})),
    "inner_hash": _VarDef(frozenset({Surface.SIG_INPUT})),
    # OUTPUT stage
    "sig": _VarDef(frozenset({Surface.OUTPUT})),
    "jwt": _VarDef(frozenset({Surface.OUTPUT})),
    "token": _VarDef(frozenset({Surface.OUTPUT})),
}

_FORMATTER = string.Formatter()


def _extract_variable_names(template: str) -> list[str]:
    return [
        field_name for _, field_name, _, _ in _FORMATTER.parse(template) if field_name is not None
    ]


def _validate_variable(name: str, surface: Surface) -> None:
    if name not in _VARIABLE_DEFINITIONS:
        raise InvalidTemplateVariable(f"unknown variable '{{{name}}}' in template")
    var_def = _VARIABLE_DEFINITIONS[name]
    if surface not in var_def.valid_surfaces:
        raise InvalidTemplateVariable(
            f"variable '{{{name}}}' is not valid in {surface.value} surface"
        )


def substitute(
    template: str,
    ctx: dict[str, str],
    *,
    surface: Surface,
    as_bytes: bool = False,
) -> str | bytes:
    """Expand template variables from ctx, validating surface scope.

    Args:
        template: Template string with {variable} placeholders.
        ctx: Mapping of variable name to value. For {path_bytes}, supply
             ctx["path"] (the path string); the bytes form is derived here.
        surface: The surface context (REST, WS, SIG_INPUT, OUTPUT).
        as_bytes: When True and the template is exactly "{path_bytes}",
                  return raw UTF-8 bytes of ctx["path"].

    Returns:
        Expanded string, or bytes when as_bytes=True and path_bytes template.

    Raises:
        InvalidTemplateVariable: On unknown variable or wrong-surface usage.
    """
    var_names = _extract_variable_names(template)
    for name in var_names:
        _validate_variable(name, surface)

    # Special handling for {path_bytes}: synthetic variable that emits bytes
    # from ctx["path"] in bytes context, or the path string in string context.
    if as_bytes and var_names == ["path_bytes"]:
        path_val = ctx.get("path", "")
        return path_val.encode("utf-8")

    # Build substitution mapping: path_bytes resolves to path value in str context
    substitution: dict[str, str] = {}
    for name in var_names:
        if name == "path_bytes":
            substitution[name] = ctx.get("path", "")
        else:
            substitution[name] = ctx.get(name, "")

    return template.format(**substitution)
