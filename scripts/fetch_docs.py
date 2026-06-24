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
  uv run scripts/fetch_docs.py --check                  # probe URLs reachable (downloads nothing)
  uv run scripts/fetch_docs.py arag__premium-2026 --apply   # download into data/inbox/ (then intake.py)
  uv run scripts/fetch_docs.py arag__premium-2026 --into-raw  # canonical: straight into data/raw/ (then ingest.py)
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
RAW = ROOT / "data" / "raw"
UA = "Mozilla/5.0 (check0r3000 fetch_docs; personal RSV comparison)"

# CHECK24 filestore "kind" -> a UNIQUE canonical doctype per tariff. tariff_terms and
# tariff_terms_extra both describe terms (AVB vs. Besondere VB); keeping them distinct
# avoids the silent filename collision the filename-guessing intake path suffers, so
# both documents survive into the extract payload.
KIND_TO_DOCTYPE = {
    "tariff_terms": "avb",
    "tariff_terms_extra": "avb_besondere",
    "tariff_infos": "produktinfoblatt",
    "tariff_concatenated_additional_documents": "weitere_unterlagen",
}


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


def raw_target_for(stem: str, doc: dict, used: set[str]) -> Path:
    """Canonical target under data/raw/<insurer>/<tariff>/<doctype>.pdf, derived from
    the stem and the doc's CHECK24 `kind` — so ingest/extract name the record exactly
    `<stem>.json` without any filename guessing. Disambiguates a repeated doctype with
    a numeric suffix so no document is lost."""
    insurer_part, _, tariff_part = stem.partition("__")
    doctype = KIND_TO_DOCTYPE.get(doc.get("kind", ""), doc.get("doctype") or "unsortiert")
    name = doctype
    i = 2
    while name in used:
        name = f"{doctype}-{i}"
        i += 1
    used.add(name)
    # stem parts are our own slugs (no slashes); still take basename defensively.
    return RAW / Path(insurer_part).name / Path(tariff_part).name / f"{name}.pdf"


def check(url: str) -> str:
    """Probe a URL for reachability + PDF content-type WITHOUT downloading the body.

    Verifies "could we download this?" cheaply: a HEAD request retrieves only the
    response headers (status, content-type, size), never the copyrighted document.
    Some filestore backends reject HEAD, so fall back to a 0-byte range GET (206,
    one byte) which is still effectively no download.
    """

    def probe(method: str, extra: dict[str, str] | None = None) -> str:
        headers = {"User-Agent": UA}
        if extra:
            headers.update(extra)
        req = urllib.request.Request(url, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=30) as resp:
            ctype = resp.headers.get_content_type()
            clen = resp.headers.get("Content-Length")
            size = f"{int(clen) // 1024} KiB" if clen and clen.isdigit() else "?"
            mark = "OK " if ctype == "application/pdf" else "?? "
            return f"{mark} {resp.status} {ctype}, {size}"

    try:
        return probe("HEAD")
    except urllib.error.HTTPError as e:
        if e.code in (403, 405, 501):  # HEAD not allowed -> tiny range GET
            try:
                return probe("GET", {"Range": "bytes=0-0"})
            except (urllib.error.URLError, ValueError, TimeoutError) as e2:
                return f"FAILED ({e2})"
        return f"FAILED (HTTP {e.code})"
    except (urllib.error.URLError, ValueError, TimeoutError) as e:
        # ValueError: malformed/scheme-less URL ("unknown url type") — not a URLError.
        return f"FAILED ({e})"


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
    ap.add_argument("--into-raw", action="store_true",
                    help="sort straight into data/raw/<stem>/<doctype>.pdf (canonical, "
                         "skips the filename-guessing intake step); implies --apply")
    ap.add_argument("--check", action="store_true",
                    help="probe each URL for reachability + PDF type (downloads nothing)")
    args = ap.parse_args()
    if args.into_raw:
        args.apply = True

    tariffs = select(load_manifest(), args.stems, args.insurer)
    if not tariffs:
        print("Nothing selected.")
        return 0

    if args.check:
        print(f"REACHABILITY CHECK — {len(tariffs)} tariff(s), no download:\n")
        ok = warn = bad = 0
        for t in tariffs:
            print(f"  {t.get('stem')}  [{t.get('insurer')} — {t.get('tariff')}]")
            for doc in t.get("docs", []):
                res = check(doc.get("url", ""))
                if res.startswith("FAILED"):
                    bad += 1  # only a network/HTTP error is a real failure
                elif res.startswith("OK"):
                    ok += 1
                else:
                    warn += 1  # reachable, but served with a non-PDF content-type
                print(f"    {(doc.get('doctype') or '?'):<20} {res}")
            print()
        extra = f" ({warn} reachable but non-PDF content-type)" if warn else ""
        print(f"{ok + warn} reachable{extra}, {bad} failed. "
              f"(No files written — headers only.)")
        return 1 if bad else 0

    if args.apply:
        (RAW if args.into_raw else INBOX).mkdir(parents=True, exist_ok=True)
    print(f"{'DOWNLOAD' if args.apply else 'DRY-RUN'} — {len(tariffs)} tariff(s)"
          f"{' (canonical -> data/raw/)' if args.into_raw else ''}:\n")
    n_docs = 0
    for t in tariffs:
        print(f"  {t.get('stem')}  [{t.get('insurer')} — {t.get('tariff')}]")
        used: set[str] = set()
        for doc in t.get("docs", []):
            dest = (raw_target_for(t.get("stem", ""), doc, used)
                    if args.into_raw else target_for(doc))
            n_docs += 1
            rel = dest.relative_to(ROOT)
            if not args.apply:
                print(f"    {doc.get('doctype')}: {doc.get('url')}\n      -> {rel}")
            elif dest.exists():
                print(f"    {doc.get('doctype')}: exists, skipped -> {rel}")
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                print(f"    {doc.get('doctype')}: {download(doc.get('url'), dest)} -> {rel}")
        print()

    if not args.apply:
        print(f"{n_docs} document(s) would be fetched. Re-run with --apply to download, "
              f"then: uv run scripts/intake.py")
    elif args.into_raw:
        print("Downloaded into data/raw/. Next: uv run scripts/ingest.py")
    else:
        print("Downloaded. Next: uv run scripts/intake.py  (sort into data/raw/)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
