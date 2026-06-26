#!/usr/bin/env -S uv run
"""Harvest a tariff's source-document URLs from the live CHECK24 result page and merge
them into data/sources/check24-documents.json — the manual step the README still
describes (paste check24_scrape.js, call check24Docs, hand-reshape the manifest), now
scripted in one headless Playwright pass.

WHY this is separate from fetch_ratings.py (the market-rows scraper): the two have
different cardinality and cadence. Rows come for ALL ~214 tariffs in one SSR read with
no clicks (cheap, run regularly to track price drift). Document URLs only exist after a
tariff's "Tarifdetails" panel is expanded (one click + ~3s lazy-load PER tariff), and
you only ever want them for the handful you actually analyze. So this targets a
selected few and stops at the manifest (or, with --download, hands the stems to
fetch_docs.py for the parallel PDF download into data/raw/).

First run — install the headless browser once (shared with fetch_ratings.py):
    uv run --with playwright playwright install chromium

Select which tariffs to harvest (against the FRESH page, since result-list positions
drift between scans — match by name, not the position you saw in the TUI):
    uv run scripts/harvest_docs.py --match "JURPRIVAT"          # one product, by name
    uv run scripts/harvest_docs.py --match "JURPRIVAT,S-Direkt" # several, comma-list
    uv run scripts/harvest_docs.py --insurer "KS/Auxilia"       # every row of an insurer
    uv run scripts/harvest_docs.py --match Rundum --insurer S-Direkt   # narrow a name to one insurer
    uv run scripts/harvest_docs.py --positions 7,12             # raw fresh-page positions
    uv run scripts/harvest_docs.py --all                        # every tariff (SLOW: 214 panels)

Add --download to also fetch the PDFs straight into data/raw/ (then ingest -> extract):
    uv run scripts/harvest_docs.py --match JURPRIVAT --download
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
MANIFEST = ROOT / "data" / "sources" / "check24-documents.json"
SCRAPE_JS = SCRIPTS / "check24_scrape.js"
HOST = "https://rechtsschutz.check24.de"

# _slug must match tui_data's (it names data/raw/ and is how the TUI resolves a row to a
# record); KIND_TO_DOCTYPE must match fetch_docs's canonical mapping so the manifest's
# doctype agrees with how fetch_docs --into-raw names the file (tariff_terms_extra ->
# avb_besondere, not the JS's lossy "avb"). Both modules are stdlib-only, no side effects.
sys.path.insert(0, str(SCRIPTS))
from tui_data import _slug  # noqa: E402
from fetch_docs import KIND_TO_DOCTYPE  # noqa: E402

READY_SELECTOR = ".result_box__content"          # present after SSR (list loaded at all)
HYDRATION_SELECTOR = ".efeedback-button__count"   # present after Vue hydrates (soft wait)
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def build_url() -> str:
    """Reuse check24_query.py to render the all-insurers result URL from the saved
    profile (the query string carries the PII quote profile; we never print it)."""
    res = subprocess.run(
        ["uv", "run", str(SCRIPTS / "check24_query.py"), "--all-insurers"],
        capture_output=True, text=True, cwd=ROOT,
    )
    url = res.stdout.strip()
    if not url or res.returncode != 0:
        sys.exit(f"check24_query.py failed:\n{res.stderr.strip()}")
    return url


def _file_from_url(url: str) -> str:
    """The CHECK24 filename (no .pdf), url-decoded — reproduces the manifest's per-doc
    `file` field from the document URL's last path segment."""
    name = unquote(Path(urlsplit(url).path).name)
    return name[:-4] if name.lower().endswith(".pdf") else name


def _row_text(r: dict) -> str:
    return f"{r.get('insurer', '')} {r.get('product', '')}"


def _parse_positions(spec: str) -> set[int]:
    out: set[int] = set()
    for tok in spec.split(","):
        tok = tok.strip()
        if tok.isdigit():
            out.add(int(tok))
        elif tok:
            print(f"  ! ignoring non-numeric position {tok!r}", file=sys.stderr)
    return out


def _collapse_by_product(ordered: list[dict]) -> list[dict]:
    """CHECK24 lists the same tariff at MANY positions (different Selbstbeteiligung /
    module variant), each with its own filestore hash — but the AVB/terms, the only
    thing we extract, are identical across them. Keep the lowest-position representative
    per (insurer, product) so we harvest ONE bundle/stem per product, not one per SB
    level. Price/SB live in the snapshot + data/offers/ overlay, never in the docs."""
    collapsed: dict[tuple[str, str], dict] = {}
    for r in ordered:
        key = ((r.get("insurer", "") or "").casefold(), (r.get("product", "") or "").casefold())
        collapsed.setdefault(key, r)  # ordered is position-sorted -> lowest position wins
    return sorted(collapsed.values(), key=lambda r: r["position"])


def resolve_rows(rows: list[dict], args) -> list[dict]:
    """Map the requested selectors onto the FRESH page rows, then collapse same-product
    duplicates. Selectors compose: --insurer narrows --match/--positions; given alone,
    each selects on its own."""
    if args.all:
        ordered = sorted((r for r in rows if r.get("position")), key=lambda r: r["position"])
        return _collapse_by_product(ordered)

    ins = args.insurer.casefold() if args.insurer else None

    def ins_ok(r: dict) -> bool:
        return ins is None or ins in (r.get("insurer", "") or "").casefold()

    picked: dict[int, dict] = {}
    if args.positions:
        want = _parse_positions(args.positions)
        hit = {r["position"] for r in rows if r.get("position") in want and ins_ok(r)}
        for r in rows:
            if r.get("position") in hit:
                picked[r["position"]] = r
        for miss in sorted(want - {r["position"] for r in rows}):
            print(f"  ! --positions {miss}: no row at that position on the fresh page",
                  file=sys.stderr)
    if args.match:
        for term in (t.strip().casefold() for t in args.match.split(",")):
            if not term:
                continue
            if args.exact:  # product name equals the term (pins exactly one product)
                hits = [r for r in rows
                        if (r.get("product", "") or "").casefold() == term and ins_ok(r)]
            else:  # substring over "insurer product"
                hits = [r for r in rows if term in _row_text(r).casefold() and ins_ok(r)]
            if not hits:
                print(f"  ! --match {term!r}: no matching row"
                      + (" (exact)" if args.exact else "")
                      + (f" for insurer {args.insurer!r}" if ins else ""), file=sys.stderr)
            for r in hits:
                picked[r["position"]] = r
    if ins is not None and not args.positions and not args.match:
        for r in rows:
            if ins_ok(r):
                picked[r["position"]] = r

    ordered = [picked[p] for p in sorted(picked)]
    return _collapse_by_product(ordered)


def _unique_stem(base: str, used: set[str], existing_stems: set[str]) -> str:
    """A new tariff whose slug collides with a different existing/seen tariff gets a
    numeric suffix, so no two manifest entries silently share a stem (which names
    data/raw/ and the extracted record)."""
    stem, i = base, 2
    while stem in used or stem in existing_stems:
        stem = f"{base}-{i}"
        i += 1
    return stem


def _bundle_matches_insurer(insurer: str, docs: list[dict]) -> bool:
    """Soft attribution sanity check: does the harvested bundle's filenames mention the
    insurer? Guards against a lazy-load race misattributing a hash to the wrong row.
    Insurers with no token >= 4 chars (e.g. 'DMB') can't be checked -> pass."""
    blob = " ".join((d.get("file") or "") + " " + (d.get("url") or "") for d in docs).lower()
    toks = [t for t in re.split(r"[^a-z0-9]+", insurer.lower()) if len(t) >= 4]
    return any(t in blob for t in toks) if toks else True


def build_entry(row: dict, group: dict, existing_by_hash: dict, existing_stems: set[str],
                used_stems: set[str], today: str) -> dict:
    """Turn one (fresh row, harvested filestore bundle) into a manifest entry. Stem
    resolution, in order, so the same tariff maps to ONE stable stem across re-harvests:
      1. this exact filestore hash is already in the manifest -> reuse its stem (matches
         the hand-authored stems of the original 10 when their PDFs are unchanged);
      2. else the product's _slug already names an entry -> reuse it (a re-harvest that
         grabbed a different variant-row/hash still lands on the same stem);
      3. else a genuinely new product -> its _slug (suffixed only on a cross-product
         slug collision, which is rare)."""
    h = group["hash"]
    slug = _slug(row["insurer"], row["product"])
    if h in existing_by_hash:
        stem = existing_by_hash[h]["stem"]
    elif slug in existing_stems or slug in used_stems:
        stem = slug
    else:
        stem = _unique_stem(slug, used_stems, existing_stems)
    used_stems.add(stem)

    docs = []
    for d in group.get("docs", []):
        url = d.get("url", "")
        kind = d.get("kind", "")
        docs.append({
            "doctype": KIND_TO_DOCTYPE.get(kind, d.get("doctype") or kind or "unsortiert"),
            "kind": kind,
            "file": _file_from_url(url),
            "url": url,
        })
    return {
        "stem": stem,
        "insurer": row["insurer"],
        "tariff": row["product"],
        "position": row["position"],
        "hash": h,
        "harvested": today,
        "docs": docs,
    }


async def harvest(url: str, args, existing_by_hash: dict, existing_stems: set[str],
                  today: str) -> tuple[list[dict], list[dict]]:
    """Load the page once, scrape rows, then expand the selected tariffs' Tarifdetails
    panels one at a time and attribute each NEWLY revealed filestore bundle to the
    position that revealed it (hash-diff: check24Docs scans ALL open panels, so only the
    hash that appears after clicking position p belongs to p)."""
    from playwright.async_api import async_playwright, TimeoutError as PwTimeout

    js = SCRAPE_JS.read_text(encoding="utf-8")
    s = urlsplit(url)
    print(f"Loading {s.scheme}://{s.netloc}{s.path} … (query redacted — carries the PII profile)")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(user_agent=UA)
        await page.goto(url, wait_until="networkidle", timeout=60_000)
        await page.wait_for_selector(READY_SELECTOR, timeout=15_000)
        try:
            await page.wait_for_selector(HYDRATION_SELECTOR, timeout=10_000)
        except PwTimeout:
            print(f"  ! {HYDRATION_SELECTOR} absent — ratings may be null "
                  "(does not affect document harvest).", file=sys.stderr)

        await page.evaluate(js)
        rows: list[dict] = await page.evaluate("() => window.check24Rows || []")
        print(f"Scraped {len(rows)} rows.")

        selected = resolve_rows(rows, args)
        if not selected:
            print("No tariff matched the selection — nothing to harvest.", file=sys.stderr)
            await browser.close()
            return rows, []
        print(f"Harvesting documents for {len(selected)} tariff(s) "
              f"(one panel + ~3s lazy-load each):")

        seen_hashes: set[str] = set()
        used_stems: set[str] = set()
        entries: list[dict] = []
        for r in selected:
            pos = r["position"]
            label = f"{r.get('insurer')} — {r.get('product')}"
            try:
                frag = await page.evaluate(
                    "async (p) => await window.check24Docs(p)", pos
                )
            except Exception as exc:  # page eval error must not abort the whole batch
                print(f"  ! pos {pos} ({label}): check24Docs failed ({exc})", file=sys.stderr)
                continue
            new = [g for g in (frag or [])
                   if g.get("hash") and g["hash"] not in seen_hashes]
            for g in new:
                seen_hashes.add(g["hash"])
            if not new:
                print(f"  ! pos {pos} ({label}): no new document bundle "
                      "(panel empty or lazy-load too slow)", file=sys.stderr)
                continue
            if len(new) > 1:
                print(f"  ! pos {pos} ({label}): {len(new)} new bundles appeared at once "
                      "(lazy-load race) — recording all, verify attribution", file=sys.stderr)
            for g in new:
                entry = build_entry(r, g, existing_by_hash, existing_stems, used_stems, today)
                if not _bundle_matches_insurer(r.get("insurer", ""), entry["docs"]):
                    print(f"  ? pos {pos} ({label}): bundle filenames don't mention the "
                          f"insurer — stem {entry['stem']!r}, verify it's the right tariff",
                          file=sys.stderr)
                entries.append(entry)
                print(f"  ✓ pos {pos} ({label}): {len(entry['docs'])} doc(s) → {entry['stem']}")

        await browser.close()
        return rows, entries


def load_manifest() -> dict:
    if MANIFEST.exists():
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            sys.exit(f"{MANIFEST.relative_to(ROOT)} is not an object — expected "
                     "{quelle, host, tariffs:[...]}.")
        data.setdefault("tariffs", [])
        return data
    return {
        "quelle": "check24 rsv vergleichsergebnis (all insurers) — harvest_docs.py",
        "host": HOST,
        "note": "Source-PDF URLs only (PDFs are third-party/copyright -> data/raw is "
                "gitignored). fetch_docs.py downloads on demand.",
        "tariffs": [],
    }


def merge(manifest: dict, entries: list[dict]) -> tuple[int, int]:
    """Merge harvested entries into the manifest, keyed by STEM (the tariff identity).
    An existing stem is refreshed in place (its docs/hash/position re-captured); a new
    stem is appended. Keying on stem (not hash) keeps a re-harvest that grabbed a
    different variant-row from appending a duplicate of the same tariff. Returns
    (added, updated)."""
    tariffs = manifest["tariffs"]
    by_stem = {t["stem"]: t for t in tariffs if t.get("stem")}
    added = updated = 0
    for e in entries:
        old = by_stem.get(e["stem"])
        if old is not None:
            old.update({"insurer": e["insurer"], "tariff": e["tariff"],
                        "position": e["position"], "hash": e["hash"],
                        "harvested": e["harvested"], "docs": e["docs"]})
            updated += 1
        else:
            tariffs.append(e)
            by_stem[e["stem"]] = e
            added += 1
    return added, updated


def write_manifest(manifest: dict) -> None:
    """Atomic write so a crash mid-write never truncates the tracked manifest."""
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(MANIFEST)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Harvest tariff document URLs from the live CHECK24 page into the manifest.")
    ap.add_argument("--match", help="comma-list of name substrings (over 'insurer product')")
    ap.add_argument("--exact", action="store_true",
                    help="match a product name EXACTLY, not as a substring (pins one tariff)")
    ap.add_argument("--insurer", help="restrict to rows whose insurer contains this")
    ap.add_argument("--positions", help="comma-list of raw fresh-page result positions")
    ap.add_argument("--all", action="store_true", help="every tariff (SLOW: 214 panels)")
    ap.add_argument("--download", action="store_true",
                    help="after merging, fetch the PDFs into data/raw/ via fetch_docs.py")
    ap.add_argument("--jobs", type=int, default=6, metavar="N",
                    help="parallel downloads when --download (default: 6)")
    args = ap.parse_args()

    if not (args.match or args.insurer or args.positions or args.all):
        ap.error("give a selection: --match, --insurer, --positions, or --all")

    today = datetime.date.today().isoformat()
    manifest = load_manifest()
    existing_by_hash = {t["hash"]: t for t in manifest["tariffs"] if t.get("hash")}
    existing_stems = {t["stem"] for t in manifest["tariffs"] if t.get("stem")}

    url = build_url()
    rows, entries = asyncio.run(harvest(url, args, existing_by_hash, existing_stems, today))
    if not rows:
        sys.exit("No rows scraped — the page may not have loaded.")
    if not entries:
        print("Nothing harvested; manifest unchanged.")
        return 1

    added, updated = merge(manifest, entries)
    write_manifest(manifest)
    stems = [e["stem"] for e in entries]
    print(f"\nManifest {MANIFEST.relative_to(ROOT)}: +{added} new, {updated} refreshed.")
    print("Stems: " + ", ".join(stems))

    if args.download:
        print("\nDownloading PDFs into data/raw/ (parallel):")
        res = subprocess.run(
            ["uv", "run", str(SCRIPTS / "fetch_docs.py"), *stems,
             "--into-raw", "--jobs", str(args.jobs)],
            cwd=ROOT,
        )
        if res.returncode != 0:
            print("  ! some downloads failed — see above.", file=sys.stderr)
            return res.returncode
        print("\nNext: uv run scripts/ingest.py  &&  uv run scripts/extract.py --model <m>")
    else:
        print(f"\nNext: uv run scripts/fetch_docs.py {' '.join(stems)} --into-raw   "
              "(then ingest -> extract)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
