#!/usr/bin/env python3
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
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARIFFS = ROOT / "out" / "tariffs"
OUT = ROOT / "out"

MODULE_LABELS = {
    "privat": "Privat", "beruf": "Beruf", "verkehr": "Verkehr",
    "wohnen_immobilien": "Wohnen/Immobilien", "internet_web": "Internet/Web",
    "steuer": "Steuer", "sozialgericht": "Sozialgericht", "verwaltungsrecht": "Verwaltung",
}


def load_tariffs() -> list[dict]:
    files = sorted(TARIFFS.glob("*.json"))
    return [json.loads(f.read_text(encoding="utf-8")) for f in files]


def module_cell(m: dict | None) -> str:
    if not m or not m.get("included"):
        return "–"
    return m.get("level") or "✓"


def fmt(v) -> str:
    return "–" if v in (None, "", []) else str(v)


def build_matrix_md(tariffs: list[dict]) -> str:
    cols = [f"{t['insurer']} — {t['tariff']}" for t in tariffs]
    head = "| Merkmal | " + " | ".join(cols) + " |\n"
    head += "|" + "---|" * (len(cols) + 1) + "\n"

    rows: list[tuple[str, list[str]]] = []
    for key, label in MODULE_LABELS.items():
        rows.append((label, [module_cell(t.get("modules", {}).get(key)) for t in tariffs]))
    cov_fields = [
        ("Versicherungssumme", "versicherungssumme"),
        ("Selbstbeteiligung", "selbstbeteiligung"),
        ("Wartezeit (Monate)", "wartezeit_monate"),
        ("Geltungsbereich", "geltungsbereich"),
        ("Laufzeit", "vertragslaufzeit"),
    ]
    for label, f in cov_fields:
        rows.append((label, [fmt(t.get("coverage", {}).get(f)) for t in tariffs]))
    rows.append(("Beitrag/Monat (EUR)", [fmt((t.get("beitrag") or {}).get("monatlich_eur")) for t in tariffs]))

    body = "".join(f"| {label} | " + " | ".join(cells) + " |\n" for label, cells in rows)
    return head + body


def build_lists_md(tariffs: list[dict]) -> str:
    out = []
    for t in tariffs:
        out.append(f"### {t['insurer']} — {t['tariff']}\n")
        for title, key in [("Leistungen", "leistungen"), ("Ausschlüsse", "ausschluesse"), ("Besonderheiten", "besonderheiten")]:
            items = t.get(key) or []
            if items:
                out.append(f"**{title}:** " + "; ".join(items) + "\n")
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
    cmd = ["claude", "-p", instruction]
    if model:
        cmd += ["--model", model]
    proc = subprocess.run(cmd, input=payload, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"claude -p failed (exit {proc.returncode}): {proc.stderr.strip()[:300]}")
    return proc.stdout.strip()


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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true", help="skip the claude -p pros/cons synthesis")
    ap.add_argument("--model", default=None)
    args = ap.parse_args()

    tariffs = load_tariffs()
    if not tariffs:
        print("error: no tariff records in out/tariffs/ — run scripts/extract.py first", file=sys.stderr)
        return 2

    md = ["# Rechtsschutzversicherung — Vergleich\n",
          f"_{len(tariffs)} Tarif(e). Fakten aus den Vertragsunterlagen; Beiträge ggf. aus check24-Ergebnisliste._\n",
          "## Leistungsmatrix\n", build_matrix_md(tariffs), "",
          "## Details je Tarif\n", build_lists_md(tariffs)]

    if not args.no_llm:
        print("  synthesizing pros/cons via claude -p ...")
        try:
            md.append(synthesize_pros_cons(tariffs, args.model))
        except Exception as e:
            print(f"    synthesis skipped: {e}", file=sys.stderr)
            md.append("## Vor- & Nachteile im Vergleich\n_(LLM-Synthese übersprungen.)_")

    text = "\n".join(md) + "\n"
    (OUT / "vergleich.md").write_text(text, encoding="utf-8")
    (OUT / "index.html").write_text(md_to_html(text), encoding="utf-8")
    print(f"  -> {(OUT / 'vergleich.md').relative_to(ROOT)}  +  index.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
