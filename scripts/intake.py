# /// script
# requires-python = ">=3.10"
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
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
INBOX = ROOT / "data" / "inbox"
RAW = ROOT / "data" / "raw"

# Filename prefix (underscored, lowercased) -> canonical doctype.
DOCTYPE_PREFIXES = [
    ("allgemeine_versicherungsbedingungen", "avb"),
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
}

# Legal-form / boilerplate tokens dropped when deriving the tariff name.
LEGAL_STOPWORDS = {
    "se", "ag", "kg", "mbh", "gmbh", "deutschland",
    "rechtsschutzversicherung", "rechtsschutzversicherungs", "versicherung",
    "versicherungs", "ag.",
}


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", unquote(s).lower()).strip("-")


def classify(pdf: Path) -> dict:
    # URL-decode (%C2%B0 -> °) and unify spaces/underscores so filenames with
    # blanks, mixed separators or download-encoding all parse the same way.
    stem = re.sub(r"[ _]+", "_", unquote(pdf.stem)).strip("_")
    low = stem.lower()

    doctype = None
    remainder = stem
    for prefix, dt in DOCTYPE_PREFIXES:
        if low.startswith(prefix):
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
    if insurer_slug is None and tokens:
        insurer_slug, insurer_kw = slug(tokens[0]), tokens[0].lower()

    tariff_tokens = [
        t for t in tokens
        if t.lower() != insurer_kw and t.lower() not in LEGAL_STOPWORDS
    ]
    tariff_slug = slug("-".join(tariff_tokens)) or "unbenannt"

    warns = []
    if doctype is None:
        warns.append("unbekannter Dokumenttyp (Prefix nicht erkannt)")
    if doctype == "versicherungsschein":
        warns.append("VERSICHERUNGSSCHEIN — kann persönliche Daten enthalten, prüfen!")
    if insurer_slug and insurer_kw not in KNOWN_INSURERS and tokens:
        warns.append(f"Versicherer geraten ('{insurer_slug}') — ggf. KNOWN_INSURERS ergänzen")

    target = RAW / (insurer_slug or "unbekannt") / tariff_slug / f"{doctype or 'unsortiert'}.pdf"
    return {"src": pdf, "doctype": doctype, "insurer": insurer_slug,
            "tariff": tariff_slug, "target": target, "warns": warns}


def import_files(paths: list[str], move: bool) -> int:
    """Copy (or move) the given files into data/inbox/, then continue to dry-run."""
    INBOX.mkdir(parents=True, exist_ok=True)
    rc = 0
    verb = "moved" if move else "copied"
    for raw in paths:
        src = Path(raw).expanduser()
        if not src.is_file():
            print(f"  ! nicht gefunden: {raw}", file=sys.stderr)
            rc = 1
            continue
        if src.suffix.lower() != ".pdf":
            print(f"  ! kein PDF, übersprungen: {src.name}", file=sys.stderr)
            rc = 1
            continue
        dest = INBOX / src.name  # keep original filename so classify() can parse it
        if move:
            shutil.move(str(src), str(dest))
        else:
            shutil.copy2(str(src), str(dest))
        print(f"  {verb} -> data/inbox/{dest.name}")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--import", dest="import_paths", nargs="+", metavar="PFAD",
                    help="Dateien (z.B. aus ~/Downloads) in data/inbox/ aufnehmen, dann Dry-Run")
    ap.add_argument("--move", action="store_true", help="beim --import verschieben statt kopieren")
    ap.add_argument("--apply", action="store_true", help="Inbox einsortieren (default: dry-run)")
    args = ap.parse_args()

    if args.import_paths:
        print(f"Import — {len(args.import_paths)} Datei(en) in die Inbox:")
        if import_files(args.import_paths, args.move):
            print("\nMindestens eine Datei konnte nicht importiert werden (s.o.).")
        print()

    INBOX.mkdir(parents=True, exist_ok=True)
    pdfs = sorted(INBOX.glob("*.pdf"))
    if not pdfs:
        print(f"Inbox leer: lege PDFs in {INBOX.relative_to(ROOT)}/ ab und führe das Script erneut aus.")
        return 0

    plans = [classify(p) for p in pdfs]

    # Detect target collisions.
    seen: dict[Path, Path] = {}
    for p in plans:
        if p["target"] in seen:
            p["warns"].append(f"KOLLISION mit {seen[p['target']].name}")
        else:
            seen[p["target"]] = p["src"]

    print(f"{'DRY-RUN' if not args.apply else 'APPLY'} — {len(plans)} Datei(en):\n")
    rc = 0
    for p in plans:
        print(f"  {p['src'].name}")
        print(f"    -> {p['target'].relative_to(ROOT)}")
        for w in p["warns"]:
            print(f"    ! {w}")
        if any("unbekannter Dokumenttyp" in w or "KOLLISION" in w for w in p["warns"]):
            rc = 1
            print("    (übersprungen)" if args.apply else "")
            continue
        if args.apply:
            p["target"].parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p["src"]), str(p["target"]))
            print("    moved")
        print()

    if not args.apply:
        print("Plan ok? Dann:  uv run scripts/intake.py --apply")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
