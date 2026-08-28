#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
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
  uv run scripts/fetch_docs.py --into-raw --refresh     # re-download only what changed upstream
"""
from __future__ import annotations

import argparse
import concurrent.futures
import functools
import http.client
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from _manifest import load_manifest
import _vertical

ROOT = Path(__file__).resolve().parent.parent
INBOX = _vertical.inbox_dir()
RAW = _vertical.raw_dir()
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


def select(tariffs: list[dict], stems: list[str], insurer: str | None) -> list[dict]:
    if stems:
        # stem is the primary key (it names data/offers/ and data/raw/); a duplicate
        # would silently shadow one entry in a dict comprehension, so reject it.
        bystem: dict[str, dict] = {}
        for t in tariffs:
            s = t.get("stem")
            if s in bystem:
                sys.exit(f"Duplicate stem {s!r} in the manifest — stems must be unique. "
                         f"Fix data/sources/check24-documents.json.")
            bystem[s] = t
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


def target_for(doc: dict, used: set[str]) -> Path:
    # Prefer the manifest's `file`, else derive from the URL's last path segment so
    # each doc keeps a distinct, CHECK24-style name (intake.py classifies by it).
    # basename only — third-party data must never write outside data/inbox/ via a
    # stray slash or "..".
    name = doc.get("file") or Path(urlsplit(doc.get("url", "")).path).name or "unnamed"
    name = Path(name).name or "unnamed"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    # The inbox is flat, so two docs sharing a filename would collide on one dest —
    # one would silently overwrite the other (and two parallel writers race on the same
    # file). Disambiguate with a numeric suffix so every doc lands distinctly.
    if name in used:
        base = name[:-4]
        i = 2
        while f"{base}-{i}.pdf" in used:
            i += 1
        name = f"{base}-{i}.pdf"
    used.add(name)
    return INBOX / name


def raw_target_for(stem: str, doc: dict, used: set[str]) -> Path:
    """Canonical target under data/raw/<insurer>/<tariff>/<doctype>.pdf, derived from
    the stem and the doc's CHECK24 `kind` — so ingest/extract name the record exactly
    `<stem>.json` without any filename guessing. Disambiguates a repeated doctype with
    a numeric suffix so no document is lost."""
    insurer_part, _, tariff_part = stem.partition("__")
    # The manifest is hand-reshaped with no schema gate, so a typo'd stem must not
    # escape data/raw/. Path(...).name does NOT neutralize '..' (Path('..').name == '..'),
    # so reject path-escaping segments explicitly rather than trust a basename.
    for seg in (insurer_part, tariff_part):
        if seg in ("", ".", "..") or "/" in seg or "\\" in seg:
            sys.exit(f"Refusing unsafe stem {stem!r}: segment {seg!r} would escape "
                     f"data/raw/. Fix data/sources/check24-documents.json.")
    doctype = KIND_TO_DOCTYPE.get(doc.get("kind", ""), doc.get("doctype") or "unsortiert")
    name = doctype
    i = 2
    while name in used:
        name = f"{doctype}-{i}"
        i += 1
    used.add(name)
    return RAW / insurer_part / tariff_part / f"{name}.pdf"


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


def remote_size(url: str) -> int | None:
    """Byte length the server declares for a URL, without downloading the body.

    This is the only cheap change-signal we have: the filestore serves no ETag and no
    Last-Modified, and a re-issued AVB keeps both its URL and its filename. Returns None
    when the size cannot be established (no Content-Length, or the probe failed), which
    callers must treat as "unknown", never as "unchanged".
    """
    for method, extra in (("HEAD", {}), ("GET", {"Range": "bytes=0-0"})):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, **extra},
                                         method=method)
            with urllib.request.urlopen(req, timeout=30) as resp:
                if method == "GET":  # 206 -> "bytes 0-0/<total>"
                    total = resp.headers.get("Content-Range", "").rpartition("/")[2]
                    return int(total) if total.isdigit() else None
                clen = resp.headers.get("Content-Length")
                return int(clen) if clen and clen.isdigit() else None
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError):
            continue
    return None


def download(url: str, dest: Path, attempts: int = 5) -> str:
    """Fetch one document, retrying the filestore's mid-transfer cutoffs.

    The server truncates bodies under load — repeated GETs of one URL stopped at
    different offsets, and the larger the document the likelier it breaks (a 4.7 MB AVB
    lost several attempts in a row). Resuming is not an option: the filestore ignores
    Range outright, answering 200 with no Content-Range and the full Content-Length, so
    every attempt must re-fetch the whole body. Backoff grows because the cutoffs cluster
    when the host is being hammered — the retry is what makes a --refresh run reliable,
    since a lost attempt otherwise leaves the STALE document in place.
    """
    last = ""
    data = b""
    ctype = ""
    clen = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as resp:
                ctype = resp.headers.get_content_type()
                clen = resp.headers.get("Content-Length")
                data = resp.read()
            break
        except (ValueError, urllib.error.HTTPError) as e:
            return f"FAILED ({e})"  # a bad URL or an HTTP status won't fix itself
        except (urllib.error.URLError, TimeoutError,
                http.client.IncompleteRead) as e:
            # Mirror check(); one bad doc must not abort the whole --apply batch.
            last = f"FAILED ({e})"
            if attempt + 1 < attempts:
                time.sleep(2.0 * (attempt + 1))
    else:
        return last
    # Guard against a truncated/empty body or an HTML error page served as 200
    # (expired link etc.) — a 0-byte file must never be written out as "ok".
    if not data:
        return "FAILED (empty body — 0 bytes)"
    # A clean short read (server closed without IncompleteRead, e.g. no chunked
    # framing) would otherwise be cached as complete and never re-fetched. If the
    # server declared a length, require the body to match it.
    if clen and clen.isdigit() and len(data) != int(clen):
        return f"FAILED (short body: got {len(data)} of {clen} bytes)"
    if ctype != "application/pdf" and data[:5] != b"%PDF-":
        return f"FAILED (not a PDF: {ctype})"
    # Atomic AND collision-safe: a per-worker UNIQUE temp in the dest dir, then replace.
    # A shared `<dest>.part` let two workers aiming at the same dest tear the file or
    # crash the 2nd replace with FileNotFoundError (which aborted the whole pool). The
    # write/replace is inside the try so a disk-full/permission error becomes a per-doc
    # FAILED string instead of an unhandled exception, and the temp is always cleaned up.
    tmp_path = None
    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), prefix=dest.name + ".",
                                        suffix=".part")
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp_path, dest)
    except OSError as e:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        return f"FAILED (write {dest.name}: {e})"
    return f"ok ({len(data) // 1024} KiB)"


def plan(tariffs: list[dict], into_raw: bool) -> list[tuple[dict, list[dict]]]:
    """Resolve every (tariff, doc) -> destination Path up front, sequentially, so the
    per-tariff doctype disambiguation (`used`) and doc order stay deterministic before
    the parallel network stage runs. Returns groups of (tariff, [doc-item, ...]) so the
    summary can print per tariff in manifest order even though downloads finish out of
    order. Each doc-item is a fresh dict the worker stores its `result` into."""
    groups: list[tuple[dict, list[dict]]] = []
    # raw targets disambiguate per tariff dir; the flat inbox disambiguates globally so
    # no two docs across tariffs collide on one inbox filename.
    inbox_used: set[str] = set()
    for t in tariffs:
        stem = t.get("stem", "")
        used: set[str] = set()
        items: list[dict] = []
        for doc in t.get("docs", []):
            dest = raw_target_for(stem, doc, used) if into_raw else target_for(doc, inbox_used)
            items.append({
                "doctype": doc.get("doctype"),
                "url": doc.get("url", ""),
                "dest": dest,
                "rel": dest.relative_to(ROOT),
            })
        groups.append((t, items))
    return groups


def _run_pool(fn, items: list[dict], jobs: int) -> None:
    """Map fn over items with a bounded thread pool and store each result back on its
    item dict. urllib's socket I/O releases the GIL, so threads give real concurrency;
    the bound keeps us polite to a third-party host. Each item is a distinct dict, so
    concurrent `item["result"] = ...` writes never collide."""
    # max_workers must be >= 1 even if the caller passes 0/garbage.
    workers = max(1, min(jobs, len(items))) if items else 1
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fn, it): it for it in items}
        for fut in concurrent.futures.as_completed(futs):
            futs[fut]["result"] = fut.result()


def _download_one(item: dict, refresh: bool = False) -> str:
    """Worker: skip an already-present file (so re-runs are cheap and resumable),
    else download it via the guarded download().

    With `refresh`, an already-present file is not trusted blindly: its size is compared
    against what the server declares and it is re-downloaded on a mismatch. Without this
    an insurer re-issuing its AVB under the same URL stays invisible forever — the file
    exists, so it is skipped, and extract.py hashes the unchanged local text.
    """
    dest: Path = item["dest"]
    if dest.exists():
        if not refresh:
            return "exists, skipped"
        local = dest.stat().st_size
        remote = remote_size(item["url"])
        if remote is None:
            return f"unknown remote size, kept ({local // 1024} KiB)"
        if remote == local:
            return f"unchanged ({local // 1024} KiB)"
        res = download(item["url"], dest)
        return res if res.startswith("FAILED") else f"CHANGED {local} -> {remote} bytes, re-downloaded"
    dest.parent.mkdir(parents=True, exist_ok=True)
    return download(item["url"], dest)


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
    ap.add_argument("--refresh", action="store_true",
                    help="re-download documents whose remote size differs from the local "
                         "copy (detects a re-issued AVB served under the same URL); "
                         "implies --apply")
    ap.add_argument("--jobs", type=int, default=6, metavar="N",
                    help="concurrent downloads/probes (default: 6; bounded for politeness)")
    args = ap.parse_args()
    if args.into_raw or args.refresh:
        args.apply = True

    tariffs = select(load_manifest()["tariffs"], args.stems, args.insurer)
    if not tariffs:
        print("Nothing selected.")
        return 0

    if args.check:
        print(f"REACHABILITY CHECK — {len(tariffs)} tariff(s), no download:\n")
        groups = plan(tariffs, args.into_raw)
        flat = [it for _, items in groups for it in items]
        _run_pool(lambda it: check(it["url"]), flat, args.jobs)
        ok = warn = bad = 0
        for t, items in groups:
            print(f"  {t.get('stem')}  [{t.get('insurer')} — {t.get('tariff')}]")
            for it in items:
                res = it["result"]
                if res.startswith("FAILED"):
                    bad += 1  # only a network/HTTP error is a real failure
                elif res.startswith("OK"):
                    ok += 1
                else:
                    warn += 1  # reachable, but served with a non-PDF content-type
                print(f"    {(it.get('doctype') or '?'):<20} {res}")
            print()
        extra = f" ({warn} reachable but non-PDF content-type)" if warn else ""
        print(f"{ok + warn} reachable{extra}, {bad} failed. "
              f"(No files written — headers only.)")
        return 1 if bad else 0

    if args.apply:
        (RAW if args.into_raw else INBOX).mkdir(parents=True, exist_ok=True)
    print(f"{'DOWNLOAD' if args.apply else 'DRY-RUN'} — {len(tariffs)} tariff(s)"
          f"{' (canonical -> data/raw/)' if args.into_raw else ''}:\n")
    groups = plan(tariffs, args.into_raw)
    n_docs = sum(len(items) for _, items in groups)

    if args.apply:
        flat = [it for _, items in groups for it in items]
        _run_pool(functools.partial(_download_one, refresh=args.refresh), flat, args.jobs)

    fails = 0
    changed = 0
    for t, items in groups:
        print(f"  {t.get('stem')}  [{t.get('insurer')} — {t.get('tariff')}]")
        for it in items:
            if not args.apply:
                print(f"    {it['doctype']}: {it['url']}\n      -> {it['rel']}")
            else:
                res = it["result"]
                if res.startswith("FAILED"):
                    fails += 1
                elif res.startswith("CHANGED"):
                    changed += 1
                print(f"    {it['doctype']}: {res} -> {it['rel']}")
        print()

    if not args.apply:
        print(f"{n_docs} document(s) would be fetched. Re-run with --apply to download, "
              f"then: uv run scripts/intake.py")
        return 0
    tail = ("Downloaded into data/raw/. Next: uv run scripts/ingest.py" if args.into_raw
            else "Downloaded. Next: uv run scripts/intake.py  (sort into data/raw/)")
    if args.refresh:
        if changed:
            tail = f"{changed} document(s) CHANGED and were re-downloaded. {tail}"
        elif fails:
            # Never claim "nothing changed" when a probe/download failed: the local copy
            # may well be stale, we just could not establish it.
            tail = f"Change status UNKNOWN for {fails} document(s) — see FAILED above. {tail}"
        else:
            tail = f"No document changed upstream. {tail}"
    print(f"{tail}  ({fails} failed)" if fails else tail)
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
