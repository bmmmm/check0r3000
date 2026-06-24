"""Trim a verbose AVB to the comparison-relevant passages.

A full Allgemeine Versicherungsbedingungen document (the ARAG one is 174 pages /
~200k tokens) does not fit the 200k context of small/cheap/local models, and most
of it — procedural clauses, data-protection boilerplate, definitions — never maps
to a comparison field. This module keeps only the passages that do.

Two deterministic, stdlib-only strategies (no model, no extra deps):

  * "window"  — keep a context window of lines around every anchor-keyword hit,
                merge overlapping windows, mark gaps with a placeholder.
  * "section" — split on `§`-headers and keep whole sections whose header or body
                mentions a topic anchor; drop the rest.

`filter_text()` runs both and returns whichever is smaller while still keeping a
configurable minimum fraction, so a document that is *all* relevant is left intact
rather than mangled. Reduction is reported via `stats()`.
"""
from __future__ import annotations

import re

# Topic anchors: the schema fields we actually compare on. Lowercase, regex.
ANCHORS = [
    r"selbstbeteiligung", r"versicherungssumme", r"warte ?zeit",
    r"geltungsbereich", r"leistungsart",
    r"privat-?rechtsschutz", r"rechtsschutz für privat", r"verkehrs-?rechtsschutz",
    r"berufs-?rechtsschutz", r"selbstständige", r"immobilie", r"wohnungs",
    r"internet", r"web@ktiv", r"steuer-?rechtsschutz", r"sozialgericht",
    r"verwaltungs-?rechtsschutz", r"mediation", r"kaution",
    r"schadenfreiheitsrabatt", r"nicht versichert", r"ausgeschlossen",
    r"ausschluss", r"vertragsdauer", r"laufzeit", r"selbstbehalt",
]
_PAT = re.compile("|".join(ANCHORS), re.IGNORECASE)
_SECTION_HEADER = re.compile(r"^\s*§\s*\d+")
GAP = "[…]"


def _window(lines: list[str], context: int) -> list[str]:
    keep = [False] * len(lines)
    for i, ln in enumerate(lines):
        if _PAT.search(ln):
            for j in range(max(0, i - context), min(len(lines), i + context + 1)):
                keep[j] = True
    out: list[str] = []
    in_gap = False
    for i, ln in enumerate(lines):
        if keep[i]:
            out.append(ln)
            in_gap = False
        elif not in_gap:
            out.append(GAP)
            in_gap = True
    return out


def _sections(lines: list[str]) -> list[str]:
    # Group lines into [header .. next header) blocks; keep blocks that hit an anchor.
    blocks: list[list[str]] = []
    cur: list[str] = []
    for ln in lines:
        if _SECTION_HEADER.match(ln) and cur:
            blocks.append(cur)
            cur = [ln]
        else:
            cur.append(ln)
    if cur:
        blocks.append(cur)
    out: list[str] = []
    for b in blocks:
        if _PAT.search("\n".join(b)):
            out.extend(b)
        elif not (out and out[-1] == GAP):
            out.append(GAP)
    return out


def filter_text(text: str, context: int = 4, min_fraction: float = 0.15) -> str:
    """Return a trimmed copy of `text`; never shrink below `min_fraction` of it.

    If BOTH strategies collapse the document below the keep-floor — which happens
    when no anchor keyword matches at all (an insurer whose wording avoids every
    term in ANCHORS) — the trimmed result is near-empty placeholder text. Feeding
    the model a content-free AVB yields an all-null record, so in that case keep the
    original intact: an oversized prompt beats a blank one.
    """
    lines = text.splitlines()
    candidates = [_window(lines, context), _sections(lines)]
    rendered = ["\n".join(c) for c in candidates]
    floor = len(text) * min_fraction
    viable = [r for r in rendered if len(r) >= floor]
    if not viable:
        return text
    return min(viable, key=len)


def stats(original: str, filtered: str) -> dict:
    return {
        "orig_chars": len(original),
        "filtered_chars": len(filtered),
        "kept_pct": round(100 * len(filtered) / max(1, len(original)), 1),
        "approx_tokens": len(filtered) // 4,
    }
