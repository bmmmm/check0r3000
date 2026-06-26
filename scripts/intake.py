#!/usr/bin/env -S uv run
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


def classify(pdf: Path) -> dict:
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
        if not is_pdf(src):
            print(f"  ! kein gültiges PDF (leer/kein %PDF-Header), übersprungen: "
                  f"{src.name}", file=sys.stderr)
            rc = 1
            continue
        dest = INBOX / src.name  # keep original filename so classify() can parse it
        if dest.exists():
            print(f"  ! existiert schon in der Inbox, übersprungen (kein Überschreiben): "
                  f"{src.name}", file=sys.stderr)
            rc = 1
            continue
        if move:
            shutil.move(str(src), str(dest))
        else:
            shutil.copy2(str(src), str(dest))
        print(f"  {verb} -> data/inbox/{dest.name}")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(
        description="PDFs in data/inbox/ aufnehmen und nach "
                    "data/raw/<versicherer>/<tarif>/<doctype>.pdf einsortieren.")
    ap.add_argument("sources", nargs="*", metavar="PFAD",
                    help="optionale Dateipfade (z.B. ~/Downloads/*.pdf), die in die Inbox "
                         "kopiert werden, bevor sortiert wird")
    ap.add_argument("--move", action="store_true",
                    help="übergebene Dateien verschieben statt kopieren")
    ap.add_argument("--apply", action="store_true",
                    help="Inbox wirklich einsortieren (default: Dry-Run)")
    args = ap.parse_args()

    if args.sources:
        print(f"Import — {len(args.sources)} Datei(en) in die Inbox:")
        if import_files(args.sources, args.move):
            print("\nMindestens eine Datei konnte nicht importiert werden (s.o.).")
        print()

    INBOX.mkdir(parents=True, exist_ok=True)
    pdfs = sorted(INBOX.glob("*.pdf"))
    if not pdfs:
        print(f"Inbox leer: lege PDFs in {INBOX.relative_to(ROOT)}/ ab und führe das Script erneut aus.")
        return 0

    plans = [classify(p) for p in pdfs]

    # Detect target collisions (within this batch) and content / on-disk problems.
    seen: dict[Path, Path] = {}
    for p in plans:
        if not is_pdf(p["src"]):
            p["warns"].append("kein gültiges PDF (leer/kein %PDF-Header)")
        if p["target"] in seen:
            p["warns"].append(f"KOLLISION mit {seen[p['target']].name}")
        else:
            seen[p["target"]] = p["src"]
        # An already-sorted file at the target must not be silently overwritten:
        # shutil.move clobbers a destination FILE, destroying a non-regenerable PDF.
        if p["target"].exists():
            p["warns"].append(f"ZIEL existiert bereits: {p['target'].relative_to(ROOT)}")

    blockers = ("unbekannter Dokumenttyp", "KOLLISION", "ZIEL existiert", "kein gültiges PDF")
    print(f"{'DRY-RUN' if not args.apply else 'APPLY'} — {len(plans)} Datei(en):\n")
    rc = 0
    for p in plans:
        print(f"  {p['src'].name}")
        print(f"    -> {p['target'].relative_to(ROOT)}")
        for w in p["warns"]:
            print(f"    ! {w}")
        if any(b in w for w in p["warns"] for b in blockers):
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
