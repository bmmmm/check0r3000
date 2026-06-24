#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pypdf>=4"]
# ///
"""Ingest stage: extract text from every source PDF and detect duplicates.

Reads:   data/raw/<insurer>/<tariff>/<doctype>.pdf
Writes:  data/extracted/<insurer>/<tariff>/<doctype>.txt
         data/extracted/manifest.json

The content hash is computed over the *extracted text*, not the file bytes, so
re-generated downloads that differ only in their PDF creation timestamp collapse
to the same hash. Identical content appearing under different tariffs is reported
as a warning — that is exactly how check24 attaches one generic document package
to several ARAG tariffs.

Run:  uv run scripts/ingest.py
"""
from __future__ import annotations

import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "extracted"


def extract_text(pdf: Path) -> tuple[str, int]:
    reader = PdfReader(str(pdf))
    pages = [(p.extract_text() or "") for p in reader.pages]
    return "\n".join(pages), len(reader.pages)


def main() -> int:
    if not RAW.is_dir():
        print(f"error: {RAW} not found. Drop PDFs into data/raw/<insurer>/<tariff>/", file=sys.stderr)
        return 2

    pdfs = sorted(RAW.glob("*/*/*.pdf"))
    if not pdfs:
        print(f"error: no PDFs under {RAW}/<insurer>/<tariff>/*.pdf", file=sys.stderr)
        return 2

    records: list[dict] = []
    by_content: dict[str, list[str]] = defaultdict(list)
    no_text: list[str] = []

    for pdf in pdfs:
        insurer, tariff = pdf.parts[-3], pdf.parts[-2]
        doctype = pdf.stem
        text, npages = extract_text(pdf)
        stripped = text.strip()
        chash = hashlib.sha256(text.encode("utf-8")).hexdigest()

        dest = OUT / insurer / tariff / f"{doctype}.txt"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")

        ident = f"{insurer}/{tariff}/{doctype}"
        records.append({
            "insurer": insurer,
            "tariff": tariff,
            "doctype": doctype,
            "pages": npages,
            "text_chars": len(stripped),
            "content_sha256": chash,
            "extracted_path": str(dest.relative_to(ROOT)),
        })
        # Only group documents that actually yielded text. A scanned/image-only PDF
        # extracts to "" and every empty one hashes identically — grouping them
        # would raise a bogus "byte-for-byte identical across tariffs" warning when
        # the real problem is that the PDF needs OCR.
        if stripped:
            by_content[chash].append(ident)
        else:
            no_text.append(ident)
        print(f"  extracted  {ident:<48} {npages:>3}p  {len(stripped):>6}ch  {chash[:12]}")

    # Duplicate report: same extracted content under more than one location.
    warnings = [
        {"content_sha256": h, "locations": locs}
        for h, locs in by_content.items() if len(locs) > 1
    ]

    manifest = {"documents": records, "duplicate_content": warnings}
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\n{len(records)} documents -> {OUT.relative_to(ROOT)}/manifest.json")
    if no_text:
        print(f"\n  WARNING: {len(no_text)} document(s) yielded NO extractable text "
              f"(scanned / image-only PDF? likely needs OCR):")
        for ident in no_text:
            print(f"    {ident}")
    if warnings:
        print(f"\n  WARNING: {len(warnings)} document(s) are byte-for-byte identical across tariffs:")
        for w in warnings:
            print(f"    {w['content_sha256'][:12]}  =  {', '.join(w['locations'])}")
        print("  -> these tariffs share a generic document package; the real differentiator")
        print("     (price, Selbstbeteiligung, modules) is likely missing from these PDFs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
