#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Render stage: build the comparison overview from the extracted tariff records.

Reads:   out/tariffs/*.json
Writes:  out/vergleich.md   (feature matrix + per-tariff pros/cons)
         out/index.html     (same content as a standalone static page)

The feature matrix is deterministic (no model). The comparative pros/cons are
inherently relative, so by default a single `claude -p` synthesis pass writes
them from the structured facts. Use --no-llm for a pure, offline render.

Run:  python3 scripts/render.py
      python3 scripts/render.py --no-llm
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _providers  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TARIFFS = ROOT / "out" / "tariffs"
ENRICHED = ROOT / "out" / "enriched"
OUT = ROOT / "out"

MODULE_LABELS = {
    "privat": "Privat", "beruf": "Beruf", "verkehr": "Verkehr",
    "wohnen_immobilien": "Wohnen/Immobilien", "internet_web": "Internet/Web",
    "steuer": "Steuer", "sozialgericht": "Sozialgericht", "verwaltungsrecht": "Verwaltung",
}


def load_records(prefer_enriched: bool) -> list[dict]:
    # Keyed by filename (the stable manifest stem), never by record content — the
    # record's insurer 'ARAG SE' is not the stem 'arag', so a content-derived key
    # would never match. When prefer_enriched, take the enriched twin (pure facts +
    # offer overlay) where it exists; otherwise always the pure record.
    records = []
    for f in sorted(TARIFFS.glob("*.json")):
        twin = ENRICHED / f.name
        src = twin if (prefer_enriched and twin.exists()) else f
        # The record is untrusted LLM JSON (extract.py only guarantees the top level
        # is *some* object); a stray list/scalar would crash the whole render and
        # leave no vergleich.md/index.html. Skip it loudly instead.
        rec = json.loads(src.read_text(encoding="utf-8"))
        if not isinstance(rec, dict):
            print(f"  skip {src.name}: top-level JSON is {type(rec).__name__}, not an object",
                  file=sys.stderr)
            continue
        records.append(rec)
    return records


def has_enriched() -> bool:
    return ENRICHED.exists() and any(ENRICHED.glob("*.json"))


def module_cell(m: dict | None) -> str:
    if not isinstance(m, dict) or not m.get("included"):
        return "–"
    lvl = m.get("level")
    return str(lvl) if lvl else "✓"


def fmt(v) -> str:
    if v in (None, "", [], {}):
        return "–"
    if isinstance(v, (list, tuple)):
        return "; ".join(str(x) for x in v)
    return str(v)


def _as_dict(v) -> dict:
    return v if isinstance(v, dict) else {}


def build_matrix_md(tariffs: list[dict]) -> str:
    # Values are untrusted LLM text: a literal '|' would split a Markdown column, and
    # wrong nested types (a string where a dict is expected) would crash the join.
    def esc(s) -> str:
        return str(s).replace("|", "\\|")

    cols = [esc(f"{t.get('insurer', '?')} — {t.get('tariff', '?')}") for t in tariffs]
    head = "| Merkmal | " + " | ".join(cols) + " |\n"
    head += "|" + "---|" * (len(cols) + 1) + "\n"

    rows: list[tuple[str, list[str]]] = []
    for key, label in MODULE_LABELS.items():
        rows.append((label, [esc(module_cell(_as_dict(t.get("modules")).get(key))) for t in tariffs]))
    cov_fields = [
        ("Versicherungssumme", "versicherungssumme"),
        ("Selbstbeteiligung", "selbstbeteiligung"),
        ("Wartezeit (Monate)", "wartezeit_monate"),
        ("Geltungsbereich", "geltungsbereich"),
        ("Laufzeit", "vertragslaufzeit"),
    ]
    for label, f in cov_fields:
        rows.append((label, [esc(fmt(_as_dict(t.get("coverage")).get(f))) for t in tariffs]))
    rows.append(("Beitrag/Monat (EUR)",
                 [esc(fmt(_as_dict(t.get("beitrag")).get("monatlich_eur"))) for t in tariffs]))

    body = "".join(f"| {label} | " + " | ".join(cells) + " |\n" for label, cells in rows)
    return head + body


def build_lists_md(tariffs: list[dict]) -> str:
    out = []
    for t in tariffs:
        out.append(f"### {t.get('insurer', '?')} — {t.get('tariff', '?')}\n")
        for title, key in [("Leistungen", "leistungen"), ("Ausschlüsse", "ausschluesse"), ("Besonderheiten", "besonderheiten")]:
            raw = t.get(key)
            items = raw if isinstance(raw, list) else ([raw] if raw else [])
            if items:
                out.append(f"**{title}:** " + "; ".join(str(x) for x in items) + "\n")
        out.append("")
    return "\n".join(out)


def synthesize_pros_cons(tariffs: list[dict], model: str | None) -> str:
    payload = json.dumps(tariffs, ensure_ascii=False, indent=2)
    instruction = (
        "You are comparing German Rechtsschutz tariffs given as JSON facts on stdin. "
        "Write a concise German Markdown section titled '## Vor- & Nachteile im Vergleich'. "
        "For each tariff, a '### <Versicherer — Tarif>' subheading with a '**Vorteile:**' "
        "bullet list and a '**Nachteile:**' bullet list, judged RELATIVE to the other "
        "tariffs. End with a short '### Fazit' naming who fits which need. "
        "Base every point strictly on the JSON facts — no invented numbers. Output only Markdown."
    )
    res = _providers.run(model or "claude", instruction, payload)
    if res["error"] or not res["text"]:
        raise RuntimeError(res["error"] or "empty response")
    return res["text"].strip()


def md_to_html(md: str) -> str:
    # Minimal, dependency-free: wrap the Markdown in <pre> inside a styled shell.
    # (Keeps the static page portable; swap for a real MD renderer if desired.)
    body = html.escape(md)
    return (
        "<!doctype html><html lang=de><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>Rechtsschutz-Vergleich</title>"
        "<style>body{font:15px/1.5 system-ui,sans-serif;max-width:60rem;margin:2rem auto;padding:0 1rem;color:#1a1a1a}"
        "pre{white-space:pre-wrap}</style></head><body><pre>" + body + "</pre></body></html>"
    )


def build_doc(tariffs: list[dict], pros_cons: str | None) -> str:
    md = ["# Rechtsschutzversicherung — Vergleich\n",
          f"_{len(tariffs)} Tarif(e). Fakten aus den Vertragsunterlagen; Beiträge ggf. aus check24-Ergebnisliste._\n",
          "## Leistungsmatrix\n", build_matrix_md(tariffs), "",
          "## Details je Tarif\n", build_lists_md(tariffs)]
    if pros_cons is not None:
        md.append(pros_cons)
    return "\n".join(md) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true", help="skip the claude -p pros/cons synthesis")
    ap.add_argument("--model", default=None)
    args = ap.parse_args()

    pure = load_records(prefer_enriched=False)
    if not pure:
        print("error: no tariff records in out/tariffs/ — run scripts/extract.py first", file=sys.stderr)
        return 2

    # Synthesize pros/cons ONCE from the pure (premium-free) facts and reuse it for
    # both renders — the comparison of coverage tradeoffs does not depend on price.
    pros_cons = None
    if not args.no_llm:
        print("  synthesizing pros/cons via claude -p ...")
        try:
            pros_cons = synthesize_pros_cons(pure, args.model)
        except Exception as e:
            print(f"    synthesis skipped: {e}", file=sys.stderr)
            pros_cons = "## Vor- & Nachteile im Vergleich\n_(LLM-Synthese übersprungen.)_"

    # Tracked, shareable deliverable: PURE facts only — never embeds a personal
    # premium/Stufe, so a routine `git add out/` cannot leak it.
    pure_doc = build_doc(pure, pros_cons)
    (OUT / "vergleich.md").write_text(pure_doc, encoding="utf-8")
    (OUT / "index.html").write_text(md_to_html(pure_doc), encoding="utf-8")
    print(f"  -> {(OUT / 'vergleich.md').relative_to(ROOT)}  +  index.html  (pure, tracked)")

    # Personal view WITH the premium/Stufe from data/offers/ — written only into the
    # gitignored out/enriched/, so it can never be committed.
    if has_enriched():
        enriched = load_records(prefer_enriched=True)
        enr_doc = build_doc(enriched, pros_cons)
        ENRICHED.mkdir(parents=True, exist_ok=True)
        (ENRICHED / "vergleich.md").write_text(enr_doc, encoding="utf-8")
        (ENRICHED / "index.html").write_text(md_to_html(enr_doc), encoding="utf-8")
        print("  -> out/enriched/vergleich.md  +  index.html  (with premium, gitignored)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
