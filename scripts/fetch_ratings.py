#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "playwright",
# ]
# ///
"""Scrape the CHECK24 result page with Playwright headless Chrome.

Waits for Vue.js to hydrate (`.efeedback-button__count` in DOM), then runs
check24_scrape.js and saves the JSON rows to tmp/rows_DATE.json.

First run — install the Chromium browser once:
    uv run --with playwright playwright install chromium

Usage:
    uv run scripts/fetch_ratings.py              # -> tmp/rows_YYYY-MM-DD.json
    uv run scripts/fetch_ratings.py --snapshot   # also call snapshot.py
    uv run scripts/fetch_ratings.py --date 2026-07-01
    uv run scripts/fetch_ratings.py --out /path/to/rows.json
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

import _scan
import _vertical

ROOT = Path(__file__).resolve().parent.parent
SCRAPE_JS = ROOT / "scripts" / "check24_scrape.js"

# Selector that appears only after Vue.js has hydrated the ratings widget.
# Used as a soft signal — if it doesn't appear, we warn and scrape anyway (SSR
# data like price/tarifnote is still there; bewertung fields will be null).
HYDRATION_SELECTOR = ".efeedback-button__count"
# Something always present after SSR (guarantees the tariff list loaded at all).
READY_SELECTOR = ".result_box__content"

# Masquerade as a real browser so CHECK24 doesn't serve a stripped hydration path.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _pyrun_cmd(script: Path, *args: str) -> list[str]:
    """Prefer the checked-out venv for sibling-script subprocesses (same
    selection as update-all.sh/pipeline.sh), fall back to uv run."""
    venv_py = ROOT / ".venv" / "bin" / "python"
    if os.access(venv_py, os.X_OK):
        return [str(venv_py), str(script), *args]
    return ["uv", "run", str(script), *args]


def build_url() -> str:
    result = subprocess.run(
        _pyrun_cmd(ROOT / "scripts" / "check24_query.py", "--all-insurers"),
        capture_output=True, text=True, cwd=ROOT,
    )
    url = result.stdout.strip()
    if not url or result.returncode != 0:
        sys.exit(f"check24_query.py failed:\n{result.stderr.strip()}")
    return url


async def scrape(url: str) -> list[dict]:
    from playwright.async_api import async_playwright, TimeoutError as PwTimeout

    js = SCRAPE_JS.read_text(encoding="utf-8")
    # flow=panel verticals (hausrat, phv): their result list uses result_tile /
    # panel cards and is VIRTUALIZED, so readiness comes from the spec's card
    # selector and the rows must be accumulated while scrolling (_scan.py). The
    # Rechtsschutz path (no spec) stays the classic one-shot SSR scrape.
    spec = _vertical.vertical_config().get("harvest") or {}
    panel = spec.get("flow") == "panel"
    ready = spec.get("card_sel") or READY_SELECTOR

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page(user_agent=USER_AGENT)

        print(f"Loading {url[:90]}...")
        # wait_until="domcontentloaded", NOT "networkidle": CHECK24 keeps a steady stream
        # of tracking/ad traffic, so the network never goes idle and networkidle
        # deadlocks until its timeout fires (same fix as harvest_docs.py). The result
        # list is SSR'd, so the ready selector below is the actual readiness signal.
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        except PwTimeout:
            sys.exit(f"page.goto timed out after 60s loading {url[:90]} — "
                      "check network connectivity or whether CHECK24 changed its response.")

        if panel:
            await page.wait_for_timeout(3500)
            await _scan.dismiss_overlays(page)

        # Wait for the SSR tariff list to be in the DOM (always present).
        await page.wait_for_selector(ready, timeout=15_000)

        if panel:
            rows: list[dict] = await _scan.accumulate_rows(page, js)
        else:
            # Wait for Vue.js to hydrate the rating widgets (soft: warn if absent).
            try:
                print(f"Waiting for Vue.js hydration ({HYDRATION_SELECTOR})...")
                await page.wait_for_selector(HYDRATION_SELECTOR, timeout=15_000)
                print("Vue hydrated — ratings should be present.")
            except PwTimeout:
                print(f"Warning: {HYDRATION_SELECTOR} did not appear — "
                      "bewertung fields will be null (headless detection or layout change).")

            await page.evaluate(js)
            rows = await page.evaluate("() => window.check24Rows || []")

        await browser.close()

    rated = sum(1 for r in rows if r.get("bewertung") is not None)
    print(f"Scraped {len(rows)} rows — {rated} with customer rating")
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch CHECK24 RSV rows via headless Chrome")
    ap.add_argument("--date", default=str(datetime.date.today()),
                    help="snapshot date label (default: today)")
    ap.add_argument("--snapshot", action="store_true",
                    help="call snapshot.py after saving rows")
    ap.add_argument("--out", metavar="PATH",
                    help="output JSON path (default: tmp/rows_DATE.json)")
    args = ap.parse_args()

    tmp = _vertical.TMP
    tmp.mkdir(exist_ok=True)
    out = Path(args.out) if args.out else tmp / f"rows_{args.date}.json"

    url = build_url()
    rows = asyncio.run(scrape(url))

    if not rows:
        sys.exit("No rows scraped — page may not have loaded or Vue did not hydrate.")

    out.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved -> {out.relative_to(ROOT)}")

    if args.snapshot:
        res = subprocess.run(
            _pyrun_cmd(ROOT / "scripts" / "snapshot.py",
                       str(out), "--date", args.date),
            cwd=ROOT,
        )
        return res.returncode

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
