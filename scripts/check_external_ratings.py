#!/usr/bin/env python3
"""check_external_ratings — staleness check for data/sources/external-ratings.json.

The external verdicts (Finanztip etc.) are hand-curated with a recorded `stand`
date. Instead of scraping the source pages (Franke & Bornberg is a JS app with
no machine-readable grades), this fetches each source URL once and greps for the
recorded stand date (ISO, German long, and German numeric form). A missing date
marker means the page has likely been updated since curation -> review the
entries by hand.

By default only finanztip.de URLs are checked (their page prints a Stand date;
F&B/test.de pages don't, checking them would always warn). --all overrides.

Run:  python3 scripts/check_external_ratings.py [--all]
Exit: 0 = all checked stands still present, 1 = at least one stale/unreachable.

Needs outbound network to the source hosts — run from a normal terminal, not
from inside a host-allowlisted sandbox.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RATINGS_PATH = REPO_ROOT / "data" / "sources" / "external-ratings.json"

GERMAN_MONTHS = [
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]


def fetch_page(url: str, timeout: int = 30) -> str:
    """One plain GET with a browser UA (shared with update_external_ratings.py)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _stand_variants(stand: str) -> list[str]:
    """The recorded ISO date in the forms a German page typically prints."""
    try:
        d = dt.date.fromisoformat(stand)
    except ValueError:
        return [stand]
    return [
        stand,                                            # 2025-08-20
        f"{d.day}. {GERMAN_MONTHS[d.month - 1]} {d.year}",  # 20. August 2025
        f"{d.day:02d}.{d.month:02d}.{d.year}",            # 20.08.2025
    ]


def _iter_entries(data: dict):
    for block in ("tariffs", "insurers"):
        table = data.get(block)
        if not isinstance(table, dict):
            continue
        for entries in table.values():
            if isinstance(entries, list):
                yield from (e for e in entries if isinstance(e, dict))
    notes = data.get("_market_notes")
    if isinstance(notes, list):
        yield from (n for n in notes if isinstance(n, dict))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--all", action="store_true",
                    help="check every source URL, not just finanztip.de")
    args = ap.parse_args()

    try:
        data = json.loads(RATINGS_PATH.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"ERROR: cannot read {RATINGS_PATH}: {e}", file=sys.stderr)
        print("  Fix or recreate the file, then re-run.", file=sys.stderr)
        return 1

    # One fetch per URL; the newest recorded stand per URL is the one to verify.
    stands_by_url: dict[str, str] = {}
    for e in _iter_entries(data):
        url, stand = e.get("url"), e.get("stand")
        if not url or not stand:
            continue
        if not args.all and "finanztip.de" not in url:
            continue
        if stand > stands_by_url.get(url, ""):
            stands_by_url[url] = stand

    if not stands_by_url:
        print("nothing to check (no matching source URLs with a stand date)")
        return 0

    stale = 0
    for url, stand in sorted(stands_by_url.items()):
        try:
            page = fetch_page(url)
        except OSError as e:
            print(f"WARN {url}: fetch failed ({e}) — check network/URL by hand")
            stale += 1
            continue
        if any(v in page for v in _stand_variants(stand)):
            print(f"ok   {url}: stand {stand} still on page")
        else:
            print(f"WARN {url}: stand {stand} not found — page likely updated,"
                  " run scripts/update_external_ratings.py to review/refresh")
            stale += 1
    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
