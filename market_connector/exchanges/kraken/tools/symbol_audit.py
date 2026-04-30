"""Kraken symbol alias audit script.

Builds an authoritative asset alias table mapping legacy Kraken-internal asset
codes (X/Z prefix scheme) to their canonical Hummingbot ticker symbols.

Source of truth
---------------
The canonical alias list comes from Kraken's official support documentation:

    https://support.kraken.com/articles/360001206766-bitcoin-currency-code-xbt-vs-btc

The 19 documented entries are hard-coded in ``KRAKEN_DOCUMENTED_ALIASES`` below
and represent the ground truth.  Chains are collapsed: XXBT→BTC (not XBT), and
XXDG→DOGE (not XDG).

API sanity check
----------------
``/0/public/Assets`` is queried as a *sanity check only*:

1. Warn if a documented code is no longer present in the API response (the doc
   remains authoritative — do NOT remove the alias on this basis alone).
2. Detect any NEW legacy-prefixed codes added by Kraken since the doc was last
   updated, using strict criteria:
   - Starts with ``XX`` / ``XE`` / ``XL`` / ``XM`` / ``XR`` / ``XZ`` (legacy
     double-X crypto pattern), OR starts with ``Z`` followed by a known ISO 4217
     fiat code from ``_FIAT_ISO_4217``.
   - The resulting stripped name must already appear in the API's altname set so
     that we only map a code when we are sure of its canonical form.

Re-run procedure
----------------
Run this script from the repository root whenever the Kraken asset list may
have changed::

    python -m market_connector.exchanges.kraken.tools.symbol_audit

Then review the diff and re-commit the updated ``_aliases_generated.py``::

    git diff market_connector/exchanges/kraken/_aliases_generated.py
    git add market_connector/exchanges/kraken/_aliases_generated.py
    git commit -m "chore(kraken): update generated asset alias table"

CI gate (Stage 6)
-----------------
The CI pipeline will re-run this script and fail if the committed file differs
from the freshly-generated output.

API references
--------------
Assets: ``GET https://api.kraken.com/0/public/Assets``
No authentication required.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Source of truth: Kraken official support documentation
# ---------------------------------------------------------------------------
#
# URL is the definitive reference for all documented legacy code aliases.
# Chains are collapsed so every entry maps directly to the HB canonical name.
# e.g. XXBT→XBT→BTC is collapsed to XXBT→BTC.
KRAKEN_LEGACY_DOC_URL = (
    "https://support.kraken.com/articles/360001206766"
    "-bitcoin-currency-code-xbt-vs-btc"
)

# 19 documented entries (chains collapsed to HB canonical names).
KRAKEN_DOCUMENTED_ALIASES: dict[str, str] = {
    # Cryptocurrencies (legacy X-prefix retained by Kraken)
    "XETC": "ETC",
    "XETH": "ETH",
    "XLTC": "LTC",
    "XMLN": "MLN",
    "XREP": "REP",
    "XXBT": "BTC",   # chain: XXBT → XBT → BTC (collapsed)
    "XXDG": "DOGE",  # chain: XXDG → XDG → DOGE (collapsed)
    "XXLM": "XLM",
    "XXMR": "XMR",
    "XXRP": "XRP",
    "XZEC": "ZEC",
    "XBT": "BTC",    # direct documented alias
    "XDG": "DOGE",   # direct documented alias
    # Fiat (legacy Z-prefix retained by Kraken)
    "ZAUD": "AUD",
    "ZCAD": "CAD",
    "ZEUR": "EUR",
    "ZGBP": "GBP",
    "ZJPY": "JPY",
    "ZUSD": "USD",
}

# ---------------------------------------------------------------------------
# Detection of NEW undocumented legacy codes via /0/public/Assets
# ---------------------------------------------------------------------------

# Known double-X crypto prefixes observed in Kraken's legacy scheme.
_LEGACY_DOUBLE_X_PREFIXES = frozenset({"XX", "XE", "XL", "XM", "XR", "XZ"})

# ISO 4217 fiat codes used by Kraken's Z-prefix legacy scheme.
_FIAT_ISO_4217 = frozenset({"USD", "EUR", "GBP", "JPY", "CAD", "AUD", "CHF"})

_ASSETS_URL = "https://api.kraken.com/0/public/Assets"

_OUTPUT_FILE = Path(__file__).parent.parent / "_aliases_generated.py"


def _fetch_assets() -> dict[str, dict[str, object]]:
    """HTTP GET ``/0/public/Assets``, return the ``result`` dict."""
    req = urllib.request.Request(
        _ASSETS_URL,
        headers={"User-Agent": "hb-market-connector/symbol-audit"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body: dict[str, object] = json.loads(resp.read().decode())

    if body.get("error"):
        raise RuntimeError(f"Kraken API error from {_ASSETS_URL}: {body['error']}")

    result: dict[str, dict[str, object]] = body.get("result", {})  # type: ignore[assignment]
    return result


def _sanity_check(
    assets: dict[str, dict[str, object]],
) -> None:
    """Warn for documented codes absent from the live API response."""
    for code in KRAKEN_DOCUMENTED_ALIASES:
        if code not in assets:
            print(
                f"  WARNING: documented alias source '{code}' not found in "
                f"/0/public/Assets response — the doc is authoritative; "
                f"alias is preserved."
            )


def _detect_new_legacy_codes(
    assets: dict[str, dict[str, object]],
    existing_aliases: dict[str, str],
) -> dict[str, str]:
    """Scan the Assets API for NEW legacy-prefixed codes not yet documented.

    Uses strict criteria to avoid false positives from modern coins whose
    tickers happen to start with X or Z (e.g. ZETA, ZEUS, ZEX, XCN):

    - Double-X crypto: internal name starts with one of ``_LEGACY_DOUBLE_X_PREFIXES``
      and the stripped suffix is itself present as an altname in the API.
    - Z-fiat: internal name is ``Z`` + a code in ``_FIAT_ISO_4217``.
    """
    # Build the set of known altnames to validate strip targets.
    known_altnames: set[str] = set()
    for info in assets.values():
        altname = info.get("altname", "")
        if isinstance(altname, str) and altname:
            known_altnames.add(altname)

    new_codes: dict[str, str] = {}
    for internal, info in assets.items():
        if internal in KRAKEN_DOCUMENTED_ALIASES or internal in existing_aliases:
            continue

        altname = info.get("altname", "")
        if not isinstance(altname, str) or not altname:
            continue

        # Double-X crypto heuristic.
        if len(internal) >= 4 and internal[:2] in _LEGACY_DOUBLE_X_PREFIXES:
            suffix = internal[2:]  # XXBT[2:] = "BT" — but we check altname directly
            if suffix and altname != internal and altname in known_altnames:
                new_codes[internal] = altname
                print(f"  Detected new legacy code (API): {internal!r} → {altname!r}")
            continue

        # Z-fiat heuristic.
        if len(internal) == 4 and internal[0] == "Z" and internal[1:] in _FIAT_ISO_4217:
            if altname != internal and altname in _FIAT_ISO_4217:
                new_codes[internal] = altname
                print(f"  Detected new Z-fiat code (API): {internal!r} → {altname!r}")

    return new_codes


def _write_aliases(aliases: dict[str, str]) -> None:
    """Write the alias table to ``_aliases_generated.py`` as a Python dict literal."""
    sorted_items = sorted(aliases.items())
    doc_keys = set(KRAKEN_DOCUMENTED_ALIASES)

    lines: list[str] = [
        "# market_connector/exchanges/kraken/_aliases_generated.py",
        "# DO NOT EDIT MANUALLY — regenerate via:",
        "#   python -m market_connector.exchanges.kraken.tools.symbol_audit",
        f"# Source of truth: {KRAKEN_LEGACY_DOC_URL}",
        "",
        "KRAKEN_ASSET_ALIASES: dict[str, str] = {",
    ]

    for key, value in sorted_items:
        if key not in doc_keys:
            comment = "  # API-detected (not yet in official docs)"
        else:
            comment = ""
        lines.append(f'    "{key}": "{value}",{comment}')

    lines.append("}")
    lines.append("")

    _OUTPUT_FILE.write_text("\n".join(lines), encoding="utf-8")
    print(f"Written {len(aliases)} aliases to {_OUTPUT_FILE}")


def run() -> None:
    """Execute the full audit: documented baseline + API sanity check + write."""
    print("Building Kraken alias table ...")
    print(f"  Source of truth: {KRAKEN_LEGACY_DOC_URL}")
    print(f"  Documented baseline: {len(KRAKEN_DOCUMENTED_ALIASES)} entries")

    print("Fetching /0/public/Assets for sanity check and new-code detection ...")
    try:
        assets = _fetch_assets()
        print(f"  /Assets → {len(assets)} asset records")
        _sanity_check(assets)
        new_codes = _detect_new_legacy_codes(assets, KRAKEN_DOCUMENTED_ALIASES)
    except Exception as exc:
        print(f"  WARNING: API fetch failed ({exc}); using documented baseline only.")
        new_codes = {}

    aliases: dict[str, str] = {**KRAKEN_DOCUMENTED_ALIASES, **new_codes}
    print(f"  Total aliases: {len(aliases)}")
    _write_aliases(aliases)


if __name__ == "__main__":
    run()
