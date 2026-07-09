#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Intake stage: sort dropped raw PDFs from data/inbox/ into the canonical layout.

Drop any check24-style PDFs into data/inbox/, then run this. It derives the
document type from the filename prefix and the insurer from a keyword table, and
proposes a target at data/raw/<insurer>/<tariff>/<doctype>.pdf.

Filenames are URL-decoded first (check24 leaves %C2%B0 etc. in download names).

Run:  uv run scripts/intake.py            # dry-run: show the sort plan
      uv run scripts/intake.py --apply     # actually move the files
"""
from __future__ import annotations

import argparse
import difflib
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import unquote

from _manifest import load_manifest

ROOT = Path(__file__).resolve().parent.parent
INBOX = ROOT / "data" / "inbox"
RAW = ROOT / "data" / "raw"

# Filename prefix (underscored, lowercased) -> canonical doctype.
DOCTYPE_PREFIXES = [
    ("allgemeine_versicherungsbedingungen", "avb"),
    ("besondere_versicherungsbedingungen", "avb_besondere"),
    ("produktinformationsblatt", "produktinfoblatt"),
    ("weitere_unterlagen", "weitere_unterlagen"),
    ("leistungsuebersicht", "leistungsuebersicht"),
    ("leistungsubersicht", "leistungsuebersicht"),
    ("leistungsübersicht", "leistungsuebersicht"),
    ("versicherungsschein", "versicherungsschein"),  # personal data — flagged
]

# Keyword found in the filename -> canonical insurer slug.
KNOWN_INSURERS = {
    "arag": "arag", "advocard": "advocard", "roland": "roland",
    "deurag": "deurag", "auxilia": "auxilia", "oerag": "oerag", "örag": "oerag",
    "concordia": "concordia", "wgv": "wgv", "huk": "huk", "allianz": "allianz",
    "itzehoer": "itzehoer", "jurpartner": "jurpartner", "nrv": "nrv",
    "neue": "nrv",  # "Neue Rechtsschutz-Versicherung"
    "ergo": "ergo", "dmb": "dmb", "adac": "adac",
    "bavariadirekt": "bavariadirekt", "bavaria": "bavariadirekt",
}

# Legal-form / boilerplate tokens dropped when deriving the tariff name.
LEGAL_STOPWORDS = {
    "se", "ag", "kg", "mbh", "gmbh", "deutschland",
    "rechtsschutzversicherung", "rechtsschutzversicherungs", "versicherung",
    "versicherungs", "ag.",
}


def slug(s: str) -> str:
    s = unquote(s).lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def is_pdf(p: Path) -> bool:
    """Cheap content gate: a real PDF is non-empty and starts with the %PDF- magic
    bytes. Guards against a 0-byte/truncated download or an HTML error page saved
    under a .pdf name (the suffix alone proves nothing)."""
    try:
        with p.open("rb") as f:
            return f.read(5) == b"%PDF-"
    except OSError:
        return False


def _closest_stems(stem: str, insurer_part: str, known_stems: set[str]) -> list[str]:
    """Suggest existing manifest stems close to a freshly minted one: same-insurer stems
    first (most likely a tariff-name typo or rename), else a fuzzy match over all known
    stems — so the user can tell a genuinely new tariff from an orphaned rename."""
    same_insurer = sorted(s for s in known_stems if s.startswith(f"{insurer_part}__"))
    if same_insurer:
        return same_insurer[:5]
    return difflib.get_close_matches(stem, sorted(known_stems), n=3, cutoff=0.4)


def classify(pdf: Path, known_stems: set[str] | None = None) -> dict:
    # URL-decode (%C2%B0 -> °) and unify spaces/underscores so filenames with
    # blanks, mixed separators or download-encoding all parse the same way.
    stem = re.sub(r"[ _]+", "_", unquote(pdf.stem)).strip("_")
    low = stem.lower()

    doctype = None
    remainder = stem
    for prefix, dt in DOCTYPE_PREFIXES:
        # Require a separator after the prefix (stem has spaces/underscores collapsed
        # to single '_'), so 'produktinformationsblattes_…' does not match and leak a
        # mangled fragment into the tariff slug.
        if low == prefix or low.startswith(prefix + "_"):
            doctype = dt
            remainder = stem[len(prefix):].lstrip("_ -")
            break

    tokens = [t for t in remainder.split("_") if t]
    insurer_slug, insurer_kw = None, None
    for t in tokens:
        key = t.lower()
        if key in KNOWN_INSURERS:
            insurer_slug, insurer_kw = KNOWN_INSURERS[key], key
            break
    if insurer_slug is None:
        # Fall back to the first NON-boilerplate token, not tokens[0]: a name starting
        # with a legal-form word ('Deutschland_…', 'Versicherung_…') would otherwise
        # become the insurer slug. If every token is boilerplate, stay None -> 'unbekannt'.
        guess = next((t for t in tokens if t.lower() not in LEGAL_STOPWORDS), None)
        if guess:
            insurer_slug, insurer_kw = slug(guess), guess.lower()

    tariff_tokens = [
        t for t in tokens
        if t.lower() != insurer_kw and t.lower() not in LEGAL_STOPWORDS
    ]
    tariff_slug = slug("-".join(tariff_tokens)) or "unbenannt"

    warns = []
    if doctype is None:
        warns.append("unknown document type (prefix not recognized)")
    if doctype == "versicherungsschein":
        warns.append("VERSICHERUNGSSCHEIN — may contain personal data, review before committing!")
    if insurer_slug and insurer_kw not in KNOWN_INSURERS and tokens:
        warns.append(f"insurer guessed ('{insurer_slug}') — consider adding it to KNOWN_INSURERS")

    insurer_part = insurer_slug or "unbekannt"
    target = RAW / insurer_part / tariff_slug / f"{doctype or 'unsortiert'}.pdf"
    # Cross-check the minted stem against the doc-URL manifest: a hand-dropped PDF for a
    # known tariff, misspelled or renamed, would otherwise mint an orphan stem that never
    # joins the pipeline (out/tariffs, the TUI) without anyone noticing. known_stems=None
    # (manifest unavailable) disables the check rather than failing intake outright.
    stem = f"{insurer_part}__{tariff_slug}"
    orphan_stem = known_stems is not None and stem not in known_stems
    if orphan_stem:
        suggestions = _closest_stems(stem, insurer_part, known_stems)
        hint = f" — closest known stem(s): {', '.join(suggestions)}" if suggestions else ""
        warns.append(f"orphan stem {stem!r}: not in data/sources/check24-documents.json"
                     f"{hint} (rename to match, or add it via harvest_docs.py — not "
                     f"auto-fixed, verify before it silently misses the pipeline)")
    return {"src": pdf, "doctype": doctype, "insurer": insurer_slug,
            "tariff": tariff_slug, "target": target, "stem": stem,
            "orphan_stem": orphan_stem, "warns": warns}


def import_files(paths: list[str], move: bool) -> int:
    """Copy (or move) the given files into data/inbox/, then continue to dry-run."""
    INBOX.mkdir(parents=True, exist_ok=True)
    rc = 0
    verb = "moved" if move else "copied"
    for raw in paths:
        src = Path(raw).expanduser()
        if not src.is_file():
            print(f"  ! not found: {raw}", file=sys.stderr)
            rc = 1
            continue
        if src.suffix.lower() != ".pdf":
            print(f"  ! not a PDF, skipped: {src.name}", file=sys.stderr)
            rc = 1
            continue
        if not is_pdf(src):
            print(f"  ! not a valid PDF (empty or missing %PDF header), skipped: "
                  f"{src.name}", file=sys.stderr)
            rc = 1
            continue
        dest = INBOX / src.name  # keep original filename so classify() can parse it
        if dest.exists():
            print(f"  ! already in the inbox, skipped (no overwrite): "
                  f"{src.name}", file=sys.stderr)
            rc = 1
            continue
        if move:
            shutil.move(str(src), str(dest))
        else:
            shutil.copy2(str(src), str(dest))
        print(f"  {verb} -> data/inbox/{dest.name}")
    return rc


def _load_known_stems() -> set[str] | None:
    """Stems already tracked in the doc-URL manifest, for classify()'s orphan check.
    A missing/malformed manifest must not block intake's core job (sorting PDFs), so
    degrade to "check disabled" instead of aborting."""
    try:
        return {t.get("stem") for t in load_manifest()["tariffs"] if t.get("stem")}
    except SystemExit as exc:
        print(f"  ! manifest unavailable ({exc}) — orphan-stem check disabled",
              file=sys.stderr)
        return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Pick up PDFs from data/inbox/ and sort them into "
                    "data/raw/<insurer>/<tariff>/<doctype>.pdf.")
    ap.add_argument("sources", nargs="*", metavar="PATH",
                    help="optional file paths (e.g. ~/Downloads/*.pdf) to copy into the "
                         "inbox before sorting")
    ap.add_argument("--move", action="store_true",
                    help="move the given files instead of copying")
    ap.add_argument("--apply", action="store_true",
                    help="actually sort the inbox (default: dry-run)")
    args = ap.parse_args()

    if args.sources:
        print(f"Import — {len(args.sources)} file(s) into the inbox:")
        if import_files(args.sources, args.move):
            print("\nAt least one file could not be imported (see above).")
        print()

    INBOX.mkdir(parents=True, exist_ok=True)
    pdfs = sorted(INBOX.glob("*.pdf"))
    if not pdfs:
        print(f"Inbox empty: drop PDFs into {INBOX.relative_to(ROOT)}/ and run this script again.")
        return 0

    known_stems = _load_known_stems()
    plans = [classify(p, known_stems) for p in pdfs]

    # Detect target collisions (within this batch) and content / on-disk problems.
    seen: dict[Path, Path] = {}
    for p in plans:
        if not is_pdf(p["src"]):
            p["warns"].append("not a valid PDF (empty or missing %PDF header)")
        if p["target"] in seen:
            p["warns"].append(f"COLLISION with {seen[p['target']].name}")
        else:
            seen[p["target"]] = p["src"]
        # An already-sorted file at the target must not be silently overwritten:
        # shutil.move clobbers a destination FILE, destroying a non-regenerable PDF.
        if p["target"].exists():
            p["warns"].append(f"TARGET already exists: {p['target'].relative_to(ROOT)}")

    blockers = ("unknown document type", "COLLISION", "TARGET already exists",
                "not a valid PDF")
    print(f"{'DRY-RUN' if not args.apply else 'APPLY'} — {len(plans)} file(s):\n")
    rc = 0
    for p in plans:
        print(f"  {p['src'].name}")
        print(f"    -> {p['target'].relative_to(ROOT)}")
        for w in p["warns"]:
            print(f"    ! {w}")
        if any(b in w for w in p["warns"] for b in blockers):
            rc = 1
            print("    (skipped)" if args.apply else "")
            continue
        if args.apply:
            p["target"].parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p["src"]), str(p["target"]))
            print("    moved")
        print()

    if not args.apply:
        print("Plan OK? Then:  uv run scripts/intake.py --apply")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
