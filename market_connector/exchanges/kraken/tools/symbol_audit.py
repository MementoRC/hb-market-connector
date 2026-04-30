"""Kraken symbol alias audit script.

Queries the Kraken public API to build an authoritative asset alias table that
maps legacy Kraken-internal asset codes (X/Z prefix scheme) to their canonical
ticker symbols.  Two sources are combined:

1. ``/0/public/Assets`` — maps each internal asset name to its ``altname``
   (canonical ticker).  This is the primary source for legacy X/Z prefix codes.
2. ``/0/public/AssetPairs`` — enumerates all trading pairs with ``base``,
   ``quote`` and ``wsname``.  We use this to discover additional aliases present
   in pair ``base``/``quote`` fields (e.g. ``XXBT`` used as a pair base but not
   listed in the Assets endpoint).

The generated table is written to
``market_connector/exchanges/kraken/_aliases_generated.py`` and committed to
the repository.

Re-run procedure
----------------
Run this script from the repository root whenever the Kraken asset list may
have changed (e.g. a new token listing)::

    python -m market_connector.exchanges.kraken.tools.symbol_audit

Then review the diff and re-commit the updated ``_aliases_generated.py``::

    git diff market_connector/exchanges/kraken/_aliases_generated.py
    git add market_connector/exchanges/kraken/_aliases_generated.py
    git commit -m "chore(kraken): update generated asset alias table"

CI gate (Stage 6)
-----------------
The CI pipeline will re-run this script and fail if the committed file differs
from the freshly-generated output.  That ensures the alias table stays in sync
with the live Kraken API.

API references
--------------
Assets:    ``GET https://api.kraken.com/0/public/Assets``
AssetPairs: ``GET https://api.kraken.com/0/public/AssetPairs``
No authentication required.
"""

from __future__ import annotations

import importlib.util
import json
import urllib.request
from pathlib import Path

_ASSETS_URL = "https://api.kraken.com/0/public/Assets"
_ASSET_PAIRS_URL = "https://api.kraken.com/0/public/AssetPairs"

# Manual HB-specific overrides — these are ALWAYS preserved across re-runs and
# take priority over API-derived names when the API also maps the same key.
# These encode HB business logic: HB uses BTC/ETH/DOGE, not Kraken's XBT/XDG.
_MANUAL_OVERRIDES: dict[str, str] = {
    "XXBT": "BTC",  # Kraken altname is "XBT"; HB canonical is "BTC"
    "XBT": "BTC",   # Kraken public ticker; HB canonical is "BTC"
    "XXDG": "DOGE",  # Kraken altname is "XDG"; HB canonical is "DOGE"
    "XDG": "DOGE",   # Kraken's internal code for Dogecoin
}

# Bootstrap skeleton covering the most common legacy codes.  The script merges
# API results on top of this so new entries appear automatically without losing
# the known-good baseline.
_SKELETON: dict[str, str] = {
    "XXBT": "BTC",
    "XBT": "BTC",
    "XETH": "ETH",
    "XLTC": "LTC",
    "XXLM": "XLM",
    "XXMR": "XMR",
    "XXRP": "XRP",
    "XZEC": "ZEC",
    "ZUSD": "USD",
    "ZEUR": "EUR",
    "ZGBP": "GBP",
    "ZJPY": "JPY",
    "ZCAD": "CAD",
}

_OUTPUT_FILE = Path(__file__).parent.parent / "_aliases_generated.py"


def _load_existing_aliases() -> dict[str, str]:
    """Load the committed alias table if it exists, preserving manual entries."""
    if not _OUTPUT_FILE.exists():
        return {}

    spec = importlib.util.spec_from_file_location(
        "kraken._aliases_generated", _OUTPUT_FILE
    )
    if spec is None or spec.loader is None:
        return {}

    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    except Exception:
        return {}

    aliases: object = getattr(module, "KRAKEN_ASSET_ALIASES", {})
    return dict(aliases) if isinstance(aliases, dict) else {}


def _fetch(url: str) -> dict[str, object]:
    """HTTP GET ``url``, parse JSON, raise on API-level errors."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "hb-market-connector/symbol-audit"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body: dict[str, object] = json.loads(resp.read().decode())

    if body.get("error"):
        raise RuntimeError(f"Kraken API error from {url}: {body['error']}")

    return body


def _fetch_asset_aliases() -> dict[str, str]:
    """Query ``/0/public/Assets`` and return internal-name → altname mappings.

    Only assets whose internal name differs from their ``altname`` are included.
    Modern bare names (e.g. ``ETH`` → ``ETH``) are skipped since they need no
    aliasing.
    """
    body = _fetch(_ASSETS_URL)
    result: dict[str, dict[str, object]] = body.get("result", {})  # type: ignore[assignment]
    aliases: dict[str, str] = {}
    for internal_name, info in result.items():
        altname = info.get("altname", "")
        if isinstance(altname, str) and altname and internal_name != altname:
            aliases[internal_name] = altname
    print(f"  /Assets  → {len(aliases)} aliased assets")
    return aliases


_LEGACY_PREFIX_RE_CRYPTO = frozenset({"XX", "XE", "XL", "XM", "XR", "XZ"})
_LEGACY_Z_FIATS = frozenset({
    "ZUSD", "ZEUR", "ZGBP", "ZJPY", "ZCAD", "ZAUD", "ZSEK", "ZDKK",
    "ZPLN", "ZNOK", "ZCHF", "ZMXN", "ZARSD", "ZCLP", "ZCOP", "ZGEL",
    "ZGHS", "ZLKR", "ZUGX", "ZVND", "ZXOF", "ZARS",
})


def _is_legacy_kraken_code(code: str) -> bool:
    """Return True if ``code`` looks like a Kraken legacy X/Z prefix code.

    Kraken's legacy scheme:
    - Double-X crypto: XXBT, XETH, XXLM, XXMR, XXRP, XZEC, XLTC, XMLN, XREP
      (first char X, second char also a letter from the canonical name)
    - Z-prefix fiat: ZUSD, ZEUR, ZGBP, etc.  (known set — avoid matching
      modern Z-ticker coins like ZETA, ZEUS, ZEX which are NOT legacy)
    - Single-X crypto directly listed in the Assets endpoint (handled there).
    """
    if len(code) < 4:
        return False
    # Strict double-X crypto: XXBT, XETH looks like XX... or starts with X
    # and is in the known Kraken assets list (handled by _fetch_asset_aliases).
    # Here we only add Z-fiat codes that are in our known fiat set.
    if code in _LEGACY_Z_FIATS:
        return True
    # Double-X prefix (e.g. XXBT, XXLM, XXMR, XXRP, XXDG)
    if code[:2] == "XX":
        return True
    return False


def _fetch_pair_aliases(asset_aliases: dict[str, str]) -> dict[str, str]:
    """Query ``/0/public/AssetPairs`` and extract additional legacy aliases.

    For each pair's ``base`` and ``quote`` fields:
    - If the code is already in ``asset_aliases`` (from the Assets endpoint),
      include it (ensures pairs using internal codes get mapped).
    - If the code looks like a Kraken legacy X/Z prefix code (via
      ``_is_legacy_kraken_code``), strip the leading X/Z to derive the
      canonical form.

    This deliberately excludes modern tickers that happen to start with X or Z
    (e.g. ZETA, ZEX, ZEUS, ZIG, XCN) to avoid spurious alias entries.
    """
    body = _fetch(_ASSET_PAIRS_URL)
    result: dict[str, dict[str, object]] = body.get("result", {})  # type: ignore[assignment]

    extra: dict[str, str] = {}
    for pair_info in result.values():
        for field in ("base", "quote"):
            code = pair_info.get(field, "")
            if not isinstance(code, str):
                continue
            # Already aliased by Assets endpoint — include it.
            if code in asset_aliases:
                extra[code] = asset_aliases[code]
                continue
            # Conservative legacy-code heuristic.
            if _is_legacy_kraken_code(code):
                stripped = code[1:]  # ZUSD → USD, XXBT → XBT
                if stripped and stripped != code:
                    extra[code] = stripped

    print(f"  /AssetPairs → {len(extra)} additional aliases from pair fields")
    return extra


def _merge_aliases(
    existing: dict[str, str],
    asset_aliases: dict[str, str],
    pair_aliases: dict[str, str],
) -> dict[str, str]:
    """Merge alias sources with precedence: manual > Assets API > pairs > skeleton.

    The ``existing`` dict is used only to preserve entries that are not
    derivable from the API or skeleton — specifically, entries that are in
    ``existing`` but NOT in any of the API or heuristic results.  This allows
    future manual additions to ``_aliases_generated.py`` to survive re-runs.
    However, entries that CAN be re-derived are always refreshed from the
    current API state (no stale data preserved).
    """
    # Compute the fully re-derived set first.
    derived: dict[str, str] = {}
    derived.update(_SKELETON)
    derived.update(pair_aliases)
    derived.update(asset_aliases)
    derived.update(_MANUAL_OVERRIDES)

    # Preserve only genuinely manual existing entries (not re-derivable).
    merged: dict[str, str] = {}
    for key, value in existing.items():
        if key not in derived:
            merged[key] = value

    # Apply re-derived on top (overwrites any stale preserved values).
    merged.update(derived)

    return merged


def _write_aliases(aliases: dict[str, str]) -> None:
    """Write the alias table to ``_aliases_generated.py`` as a Python dict literal."""
    sorted_items = sorted(aliases.items())

    lines: list[str] = [
        "# market_connector/exchanges/kraken/_aliases_generated.py",
        "# DO NOT EDIT MANUALLY — regenerate via:",
        "#   python -m market_connector.exchanges.kraken.tools.symbol_audit",
        "",
        "KRAKEN_ASSET_ALIASES: dict[str, str] = {",
    ]

    for key, value in sorted_items:
        comment = "  # HB-specific override (preserved by audit)" if key in _MANUAL_OVERRIDES else ""
        lines.append(f'    "{key}": "{value}",{comment}')

    lines.append("}")
    lines.append("")

    _OUTPUT_FILE.write_text("\n".join(lines), encoding="utf-8")
    print(f"Written {len(aliases)} aliases to {_OUTPUT_FILE}")


def run() -> None:
    """Execute the full audit: fetch from both endpoints, merge, write."""
    print(f"Fetching asset data from Kraken API ...")
    existing = _load_existing_aliases()
    print(f"  Loaded {len(existing)} existing aliases from committed file.")

    asset_aliases = _fetch_asset_aliases()
    pair_aliases = _fetch_pair_aliases(asset_aliases)

    merged = _merge_aliases(existing, asset_aliases, pair_aliases)
    print(f"  Merged total: {len(merged)} aliases.")

    _write_aliases(merged)


if __name__ == "__main__":
    run()
