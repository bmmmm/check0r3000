#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pypdf>=4",
# ]
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

A PDF is skipped (no re-parse) when its .txt is already newer than the PDF itself;
use --force to re-extract everything regardless.

Run:  uv run scripts/ingest.py
      uv run scripts/ingest.py --force
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

from pypdf import PdfReader

import _vertical

ROOT = Path(__file__).resolve().parent.parent
RAW = _vertical.raw_dir()
OUT = _vertical.extracted_dir()


def extract_text(pdf: Path) -> tuple[str, int]:
    reader = PdfReader(str(pdf))
    pages = [(p.extract_text() or "") for p in reader.pages]
    return "\n".join(pages), len(reader.pages)


def _page_count(pdf: Path) -> int:
    """Page count without the expensive per-page text decoding (used on skip)."""
    return len(PdfReader(str(pdf)).pages)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true",
                     help="re-extract every PDF even if its .txt is already up to date")
    args = ap.parse_args()

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
    failed: list[str] = []
    n_extracted = 0
    n_skipped = 0

    for pdf in pdfs:
        insurer, tariff = pdf.parts[-3], pdf.parts[-2]
        doctype = pdf.stem
        ident = f"{insurer}/{tariff}/{doctype}"
        dest = OUT / insurer / tariff / f"{doctype}.txt"

        # Incremental skip: if the extracted .txt is already newer than its source PDF,
        # re-running pypdf's per-page text decoding would reproduce the exact same
        # bytes — reuse it instead. --force bypasses this for a full re-extract.
        was_skipped = not args.force and dest.exists() and dest.stat().st_mtime >= pdf.stat().st_mtime
        if was_skipped:
            text = dest.read_text(encoding="utf-8")
            try:
                npages = _page_count(pdf)
            except Exception:
                npages = 0
            n_skipped += 1
        else:
            # Isolate per-file failures: one encrypted/corrupt/0-byte PDF must not abort
            # the whole batch (mirroring fetch_docs/check) and lose the manifest for every
            # other document. pypdf raises a wide family (FileNotDecryptedError,
            # EmptyFileError, PdfReadError, ...) — name the file so the error is actionable.
            try:
                text, npages = extract_text(pdf)
            except Exception as e:  # noqa: BLE001
                print(f"  SKIPPED    {ident:<48} extract failed: {type(e).__name__}: {e}",
                      file=sys.stderr)
                failed.append(ident)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8")
            n_extracted += 1

        stripped = text.strip()
        chash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        # Dedup grouping keys on the *stripped* text, so two copies of one generic
        # package that differ only in trailing page whitespace still group; the record
        # keeps the raw-text hash (extract.py caches on it, whitespace-sensitive).
        dhash = hashlib.sha256(stripped.encode("utf-8")).hexdigest()

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
        # would raise a bogus "identical across tariffs" warning when the real problem
        # is that the PDF needs OCR.
        if stripped:
            by_content[dhash].append(ident)
        else:
            no_text.append(ident)
        if not was_skipped:
            print(f"  extracted  {ident:<48} {npages:>3}p  {len(stripped):>6}ch  {chash[:12]}")

    # Duplicate report: same extracted content under more than one location.
    warnings = [
        {"content_sha256": h, "locations": locs}
        for h, locs in by_content.items() if len(locs) > 1
    ]

    manifest = {"documents": records, "duplicate_content": warnings}
    # Atomic write: a kill mid-write would otherwise truncate the only manifest into
    # invalid JSON that crashes every downstream json.load (extract.py / eval.py).
    manifest_path = OUT / "manifest.json"
    tmp = manifest_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(manifest_path)

    print(f"\n{len(records)} documents -> {OUT.relative_to(ROOT)}/manifest.json  "
          f"({n_extracted} extracted, {n_skipped} skipped (up to date), {len(failed)} failed)")
    if failed:
        print(f"\n  WARNING: {len(failed)} document(s) could not be read and were "
              f"skipped (encrypted, corrupt, or 0-byte — decrypt or re-download):")
        for ident in failed:
            print(f"    {ident}")
    if no_text:
        print(f"\n  WARNING: {len(no_text)} document(s) yielded NO extractable text "
              f"(scanned / image-only PDF? likely needs OCR):")
        for ident in no_text:
            print(f"    {ident}")
    if warnings:
        print(f"\n  WARNING: {len(warnings)} document(s) have identical extracted "
              f"content (whitespace-normalized) across tariffs:")
        for w in warnings:
            print(f"    {w['content_sha256'][:12]}  =  {', '.join(w['locations'])}")
        print("  -> these tariffs share a generic document package; the real differentiator")
        print("     (price, Selbstbeteiligung, modules) is likely missing from these PDFs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
