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

ROOT = Path(__file__).resolve().parent.parent
TAXONOMY_PATH = ROOT / "config" / "coverage_taxonomy.json"

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
    p = Path(path) if path else TAXONOMY_PATH
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

# (text, kind, expected_key). Same-benefit items worded differently by three
# insurers MUST collapse to one key — that is the whole point of the feature.
# Includes a precedence guard (Mobiler Anwalt must not be eaten by freie Anwaltswahl).
_ALIGNMENT_CASES = [
    # telefonische Rechtsberatung — identical service, three brandings
    ("telefonische Rechtsberatung", "leistung", "telefonische_rechtsberatung"),
    ("telefonische Rechtsberatung (ARAG JuraTel®)", "leistung", "telefonische_rechtsberatung"),
    ("telefonische Rechtsberatung (DMB-Hotline)", "leistung", "telefonische_rechtsberatung"),
    # Mediation — three different limit phrasings
    ("Mediation (max. 180 €/Std.)", "leistung", "mediation"),
    ("Mediation bis 3.000 €/Fall, 6.000 €/Jahr", "leistung", "mediation"),
    ("Mediation bis 3.000 EUR/Verfahren", "leistung", "mediation"),
    # Strafkaution
    ("Strafkaution als zinsloses Darlehen", "leistung", "strafkaution_darlehen"),
    ("Strafkaution als Darlehen bis 1 Mio EUR", "leistung", "strafkaution_darlehen"),
    # precedence guard + neighbour
    ("Mobiler Anwalt (Hausbesuch)", "leistung", "mobiler_anwalt_hausbesuch"),
    ("freie Anwaltswahl", "leistung", "freie_anwaltswahl"),
    # Übersetzung/Dolmetscher
    ("Übersetzung und Dolmetscher im Ausland", "leistung", "uebersetzung_dolmetscher"),
    ("Übersetzungskosten im Ausland", "leistung", "uebersetzung_dolmetscher"),
    ("Gebärdendolmetscher", "leistung", "uebersetzung_dolmetscher"),
    # PV / renewables, two namings
    ("erneuerbare Energien/E-Ladestation (bis 25.000 €)", "leistung", "erneuerbare_energien_pv"),
    ("Solar-/PV-Anlagen-Rechtsschutz bis 25.000 EUR", "leistung", "erneuerbare_energien_pv"),
    # Beratung Familien-/Erbrecht (hyphen vs space vs compound)
    ("Erstberatung Familien-/Erbrecht (bis 1.000 €)", "leistung", "beratung_familien_erbrecht"),
    ("erweiterter Familien-/Erbrecht-Beratungsschutz bis 1.500 EUR", "leistung", "beratung_familien_erbrecht"),
    # ROLAND digital extras — online-safety + digital-estate variants that previously
    # fell out of the taxonomy (undercounting leistung_cov). Map to the existing
    # online-monitoring / Vorsorge categories.
    ("Online-Schutz-Radar", "leistung", "identity_protection"),
    ("Webseiten-Prüfung", "leistung", "identity_protection"),
    ("Digital-Nachlass", "leistung", "vorsorge_testaments_assistent"),
    # --- exclusions ---
    ("Baufinanzierung / Kauf bebaubarer Grundstücke", "ausschluss", "ausschluss_baufinanzierung"),
    ("Baufinanzierung und Errichtung/Kauf bebaubarer Grundstücke", "ausschluss", "ausschluss_baufinanzierung"),
    ("Baurisiko (Bau/Erwerb/Finanzierung von Gebäuden)", "ausschluss", "ausschluss_baufinanzierung"),
    ("Kapitalanlagen (Erwerb/Verwaltung/Finanzierung)", "ausschluss", "ausschluss_kapitalanlage"),
    ("Kapitalanlagegeschäfte", "ausschluss", "ausschluss_kapitalanlage"),
    ("Kapitalanlagen (nur eingeschränkt gedeckt)", "ausschluss", "ausschluss_kapitalanlage"),
    ("Patent-/Marken-/Urheberrecht / geistiges Eigentum", "ausschluss", "ausschluss_geistiges_eigentum"),
    ("Patent-, Marken-, Urheber-, Designrecht (geistiges Eigentum)", "ausschluss", "ausschluss_geistiges_eigentum"),
    ("Patent-/Marken-/Urheberrecht", "ausschluss", "ausschluss_geistiges_eigentum"),
    ("Asyl- und Ausländerrecht", "ausschluss", "ausschluss_asyl_auslaenderrecht"),
    ("Asyl-/Ausländerrecht", "ausschluss", "ausschluss_asyl_auslaenderrecht"),
    ("Familien-/Erbrecht (außer § 2k Beratung/Scheidung begrenzt)", "ausschluss", "ausschluss_familien_erbrecht"),
    ("Familien-/Lebenspartnerschafts-/Erbrecht (nur Beratungs-Rechtsschutz)", "ausschluss", "ausschluss_familien_erbrecht"),
    ("Verfassungs- und internationale Gerichte", "ausschluss", "ausschluss_verfassung_internationale_gerichte"),
    ("Verfassungsgerichte und internationale Gerichtshöfe", "ausschluss", "ausschluss_verfassung_internationale_gerichte"),
    ("Spiel-/Wett-/Spekulationsgeschäfte", "ausschluss", "ausschluss_spiel_wett_spekulation"),
    ("Spiel-/Wettverträge, Gewinnzusagen", "ausschluss", "ausschluss_spiel_wett_spekulation"),
    ("Kartell- und Wettbewerbsrecht", "ausschluss", "ausschluss_kartell_wettbewerb"),
    ("Enteignung/Planfeststellung/Baurecht (BauGB)", "ausschluss", "ausschluss_baurecht_enteignung"),
    ("Kryptowährungen", "ausschluss", "ausschluss_krypto"),
]


def _selftest() -> int:
    import json as _json

    tax = load_taxonomy()
    failures = []
    for text, kind, expected in _ALIGNMENT_CASES:
        got = classify(text, kind, tax)
        if got != expected:
            failures.append(f"  {kind:<9} {text!r}\n      expected {expected!r}, got {got!r}")
    print(f"alignment cases: {len(_ALIGNMENT_CASES) - len(failures)}/{len(_ALIGNMENT_CASES)} pass")
    for f in failures:
        print("FAIL\n" + f)

    # Coverage report over the real records (no hard floor — unmatched items are
    # legitimate and surface as 'Sonstige' in the TUI; we just want visibility and
    # a guarantee that classify() never raises on real data).
    tariffs_dir = ROOT / "out" / "tariffs"
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
    ap.add_argument("--classify", nargs=2, metavar=("KIND", "TEXT"),
                    help="classify one item: KIND is 'leistung' or 'ausschluss'")
    a = ap.parse_args()
    if a.classify:
        print(classify(a.classify[1], a.classify[0]) or "(Sonstige)")
        raise SystemExit(0)
    raise SystemExit(_selftest())

