#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Download tariff source PDFs that were persisted as URLs, on demand.

We keep only the *URLs* of the AVB / Produktinfoblatt / weitere Unterlagen in
`data/sources/check24-documents.json` (harvested by scripts/check24_scrape.js) — not the
copyrighted PDFs. This script fetches a selected subset into `data/inbox/`, keeping the
CHECK24 filename so `scripts/intake.py` can classify it. Default is a DRY-RUN; pass
--apply to actually download (third-party copyright — fetch only what you need).

Run:
  uv run scripts/fetch_docs.py                          # dry-run, all tariffs in manifest
  uv run scripts/fetch_docs.py arag__premium-2026       # dry-run, one tariff (by stem)
  uv run scripts/fetch_docs.py --insurer ADVOCARD        # dry-run, one insurer
  uv run scripts/fetch_docs.py arag__premium-2026 --apply   # download, then run intake.py
"""
from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit
import json

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "data" / "sources" / "check24-documents.json"
INBOX = ROOT / "data" / "inbox"
UA = "Mozilla/5.0 (check0r3000 fetch_docs; personal RSV comparison)"


def load_manifest() -> list[dict]:
    if not MANIFEST.exists():
        sys.exit(f"No manifest at {MANIFEST.relative_to(ROOT)} — run the browser harvest "
                 f"first (scripts/check24_scrape.js -> check24Docs).")
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if isinstance(data, list):
        sys.exit(f"{MANIFEST.relative_to(ROOT)} is a bare list (raw check24Docs() output, "
                 f"grouped by hash). Wrap it as {{\"tariffs\": [{{stem, insurer, tariff, "
                 f"docs:[{{doctype, url}}]}}]}} — see data/offers/README.md.")
    return data.get("tariffs", [])


def select(tariffs: list[dict], stems: list[str], insurer: str | None) -> list[dict]:
    if stems:
        bystem = {t.get("stem"): t for t in tariffs}
        picked, missing = [], []
        for s in stems:
            (picked.append(bystem[s]) if s in bystem else missing.append(s))
        if missing:
            avail = ", ".join(sorted(t.get("stem", "?") for t in tariffs)) or "(none)"
            sys.exit(f"Unknown stem(s): {', '.join(missing)}.\nAvailable: {avail}")
        return picked
    if insurer:
        low = insurer.lower()
        return [t for t in tariffs if low in (t.get("insurer") or "").lower()]
    return tariffs


def target_for(doc: dict) -> Path:
    # Prefer the manifest's `file`, else derive from the URL's last path segment so
    # each doc keeps a distinct, CHECK24-style name (intake.py classifies by it).
    # basename only — third-party data must never write outside data/inbox/ via a
    # stray slash or "..".
    name = doc.get("file") or Path(urlsplit(doc.get("url", "")).path).name or "unnamed"
    name = Path(name).name or "unnamed"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return INBOX / name


def download(url: str, dest: Path) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            ctype = resp.headers.get_content_type()
            data = resp.read()
    except (urllib.error.URLError, TimeoutError) as e:
        return f"FAILED ({e})"
    # Guard against an HTML error page served as 200 (expired link etc.).
    if ctype != "application/pdf" and data[:5] != b"%PDF-":
        return f"FAILED (not a PDF: {ctype})"
    tmp = dest.with_suffix(dest.suffix + ".part")  # atomic: don't leave a half file
    tmp.write_bytes(data)
    tmp.replace(dest)
    return f"ok ({len(data) // 1024} KiB)"


def main() -> int:
    ap = argparse.ArgumentParser(description="Download persisted tariff source PDFs into data/inbox/.")
    ap.add_argument("stems", nargs="*", help="tariff stems to fetch (default: all in the manifest)")
    ap.add_argument("--insurer", help="fetch every tariff whose insurer name contains this")
    ap.add_argument("--apply", action="store_true", help="actually download (default: dry-run)")
    args = ap.parse_args()

    tariffs = select(load_manifest(), args.stems, args.insurer)
    if not tariffs:
        print("Nothing selected.")
        return 0

    if args.apply:
        INBOX.mkdir(parents=True, exist_ok=True)
    print(f"{'DOWNLOAD' if args.apply else 'DRY-RUN'} — {len(tariffs)} tariff(s):\n")
    n_docs = 0
    for t in tariffs:
        print(f"  {t.get('stem')}  [{t.get('insurer')} — {t.get('tariff')}]")
        for doc in t.get("docs", []):
            dest = target_for(doc)
            n_docs += 1
            rel = dest.relative_to(ROOT)
            if not args.apply:
                print(f"    {doc.get('doctype')}: {doc.get('url')}\n      -> {rel}")
            elif dest.exists():
                print(f"    {doc.get('doctype')}: exists, skipped -> {rel}")
            else:
                print(f"    {doc.get('doctype')}: {download(doc.get('url'), dest)} -> {rel}")
        print()

    if not args.apply:
        print(f"{n_docs} document(s) would be fetched. Re-run with --apply to download, "
              f"then: uv run scripts/intake.py")
    else:
        print("Downloaded. Next: uv run scripts/intake.py  (sort into data/raw/)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
