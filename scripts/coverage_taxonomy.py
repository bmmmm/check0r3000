#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Deterministic naming-normalization for the cross-tariff coverage comparison.

The Vergleich (Diff) view must line up the SAME benefit or exclusion even when two
insurers name it differently — e.g. "telefonische Rechtsberatung" vs
"telefonische Rechtsberatung (ARAG JuraTel®)" vs "telefonische Rechtsberatung
(DMB-Hotline)". It does this WITHOUT any model call at view time: a curated taxonomy
(config/coverage_taxonomy.json) maps each free-text leistung/ausschluss to a canonical
category by normalized-substring match. Pure stdlib, deterministic, fast — importable
from scripts/tui.py and exercised by scripts/regression.py.

The taxonomy is the single source of truth for the naming map; extend it by appending
one category object (no code change). Category order in the file is match precedence:
the FIRST category whose any synonym is a substring of the normalized item wins, so put
specific multi-word synonyms before generic stems.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

import _vertical

ROOT = _vertical.ROOT

# Leading list/status glyphs the TUI or source data may prepend to an item.
_LEADING_GLYPHS = "✓✗★•·–— -"
_TRADEMARK = str.maketrans("", "", "®™")

# kind -> the taxonomy list it matches against. Keeping benefits and exclusions in
# separate lists is what stops a benefit keyword from ever matching an exclusion (e.g.
# "Urheberrecht" is a benefit for DMB's internet advice but an exclusion elsewhere).
_LIST_FOR_KIND = {
    "leistung": "benefit_categories",
    "ausschluss": "exclusion_categories",
}


@lru_cache(maxsize=4)
def load_taxonomy(path: str | None = None) -> dict:
    """Load and cache the taxonomy JSON. `path` overrides the default (used by tests)."""
    p = Path(path) if path else _vertical.taxonomy_path()
    return json.loads(p.read_text(encoding="utf-8"))


def normalize(s: str) -> str:
    """Casefold and strip noise so a synonym substring-matches reliably.

    Removes trademark marks (® ™) and any leading status/list glyph, then collapses
    whitespace. Umlauts are kept verbatim — the taxonomy carries both ä and ae
    spellings, so no folding is needed (and folding would break ä-only synonyms).
    The bracketed brand suffix (e.g. "(ARAG JuraTel®)") is intentionally NOT stripped:
    it only adds signal for substring matching and never removes a needed match.
    """
    s = (s or "").translate(_TRADEMARK).casefold()
    s = s.lstrip(_LEADING_GLYPHS)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _categories(kind: str, tax: dict) -> list[dict]:
    return tax.get(_LIST_FOR_KIND.get(kind, ""), [])


def classify(text: str, kind: str, tax: dict | None = None) -> str | None:
    """Map one free-text item to its canonical category key, or None if unmatched.

    `kind` is "leistung" or "ausschluss". First category (in file order) with any
    synonym appearing as a substring of the normalized text wins. None means the
    caller should surface the item in a per-tariff "Sonstige" bucket — items are
    never silently dropped.
    """
    tax = tax or load_taxonomy()
    norm = normalize(text)
    if not norm:
        return None
    for cat in _categories(kind, tax):
        for syn in cat["synonyms"]:
            if syn in norm:
                return cat["key"]
    return None


def ordered_keys(kind: str, tax: dict | None = None) -> list[str]:
    """Canonical category keys for a kind, in file (precedence) order."""
    tax = tax or load_taxonomy()
    return [c["key"] for c in _categories(kind, tax)]


def category_label(key: str, tax: dict | None = None) -> str:
    """German display label for a category key (falls back to the key itself)."""
    tax = tax or load_taxonomy()
    for grp in ("benefit_categories", "exclusion_categories"):
        for cat in tax.get(grp, []):
            if cat["key"] == key:
                return cat["label_de"]
    return key


# ---------------------------------------------------------------------------
# Self-test: pin the cross-tariff alignments that PROVE naming-normalization,
# then report match coverage over the real records. Run: python3 scripts/
# coverage_taxonomy.py --selftest
# ---------------------------------------------------------------------------

# The alignment cases live in the per-vertical taxonomy JSON (alignment_cases:
# [text, kind, expected_key] triples) — they pin vertical-specific naming
# alignments, so they are curated DATA, not code. A freshly scaffolded
# vertical has none yet; the selftest then runs the coverage report only.


def _selftest() -> int:
    import json as _json

    tax = load_taxonomy()
    failures = []
    cases = tax.get("alignment_cases") or []
    for text, kind, expected in cases:
        got = classify(text, kind, tax)
        if got != expected:
            failures.append(f"  {kind:<9} {text!r}\n      expected {expected!r}, got {got!r}")
    if cases:
        print(f"alignment cases: {len(cases) - len(failures)}/{len(cases)} pass")
    else:
        print("(no pinned alignment_cases in this vertical's taxonomy yet "
              "— coverage report only)")
    for f in failures:
        print("FAIL\n" + f)

    # Coverage report over the real records (no hard floor — unmatched items are
    # legitimate and surface as 'Sonstige' in the TUI; we just want visibility and
    # a guarantee that classify() never raises on real data).
    tariffs_dir = _vertical.tariffs_dir()
    records = sorted(tariffs_dir.glob("*.json")) if tariffs_dir.is_dir() else []
    total = matched = 0
    sonstige = []
    for path in records:
        if path.name.startswith("_"):
            continue
        data = _json.loads(path.read_text(encoding="utf-8"))
        for kind, fieldname in (("leistung", "leistungen"), ("ausschluss", "ausschluesse")):
            for item in data.get(fieldname, []):
                total += 1
                key = classify(item, kind, tax)
                if key:
                    matched += 1
                else:
                    sonstige.append(f"  [{path.stem}] {kind}: {item}")
    if total:
        print(f"coverage over {len(records)} record(s): {matched}/{total} items mapped "
              f"({100 * matched // total}%), {len(sonstige)} -> Sonstige")
        for s in sonstige:
            print(s)

    if failures:
        print(f"\nTAXONOMY SELFTEST FAILED: {len(failures)} alignment case(s) wrong.")
        return 1
    print("\nTAXONOMY SELFTEST OK")
    return 0


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Coverage taxonomy matcher + self-test.")
    ap.add_argument("--selftest", action="store_true",
                    help="assert cross-tariff alignments and report match coverage")
    ap.add_argument("--all-verticals", action="store_true",
                    help="run the selftest once per non-disabled registry vertical "
                         "(each in a subprocess with CHECK0R_VERTICAL set); the "
                         "worst return code wins")
    ap.add_argument("--classify", nargs=2, metavar=("KIND", "TEXT"),
                    help="classify one item: KIND is 'leistung' or 'ausschluss'")
    a = ap.parse_args()
    if a.classify:
        print(classify(a.classify[1], a.classify[0]) or "(Sonstige)")
        raise SystemExit(0)
    if a.all_verticals:
        import sys
        raise SystemExit(_vertical.run_per_vertical(
            [sys.executable, __file__, "--selftest"]))
    raise SystemExit(_selftest())

