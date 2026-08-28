#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Fast market scan: read the CHECK24 result list over plain HTTP, no browser.

The result list is server-rendered — price, Tarifnote, Selbstbeteiligung, Deckungssumme
and Wartezeit are all in the initial HTML. Only the customer rating (`bewertung`) is a
Vue placeholder that needs hydration, which is the sole reason fetch_ratings.py starts
headless Chromium at all.

That matters because the market barely moves: across the first five snapshots there were
13 price changes over 214 tariffs in six weeks, and the last two full scans found zero
rating changes. Paying for a browser launch (plus a sandbox bypass, since Chromium
cannot run inside it) on every price check is the wrong trade.

This script does the same job in ~3 seconds, inside the sandbox, and carries the ratings
forward from the previous snapshot — stamped with `bewertung_stand` so a stale rating is
visible as such rather than silently passing for fresh. Ratings are NOT dropped to null:
they feed the Magic score (weight 0.05), which would tilt on every quick scan.

Run a full scan (scripts/fetch_ratings.py) when the ratings themselves should be
refreshed — monthly is ample.

Run:
    uv run scripts/fetch_prices.py                 # -> tmp/rows_YYYY-MM-DD.json
    uv run scripts/fetch_prices.py --snapshot      # also write data/snapshots/DATE.json
    uv run scripts/fetch_prices.py --compare       # parse and diff, write nothing
"""
from __future__ import annotations

import argparse
import datetime
import http.client
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from html import unescape
from pathlib import Path

import _vertical

ROOT = Path(__file__).resolve().parent.parent
SNAPDIR = _vertical.snapshots_dir()

# Same UA as the Playwright path: CHECK24 serves a stripped page to unknown agents.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
# One result row begins at the block that carries the tariff/provider ids.
ROW_SPLIT = re.compile(r'(?=<div class="tariff_and_provider")')


def build_url() -> str:
    """Ask check24_query.py for the profile URL.

    Invoked with THIS interpreter rather than `uv run`: inside the Claude Code sandbox
    uv cannot open its cache, and the whole point of this script is to run there.
    """
    for argv in ([sys.executable, str(ROOT / "scripts" / "check24_query.py")],
                 ["uv", "run", str(ROOT / "scripts" / "check24_query.py")]):
        try:
            res = subprocess.run([*argv, "--all-insurers"],
                                 capture_output=True, text=True, cwd=ROOT)
        except OSError:
            continue
        url = res.stdout.strip()
        if url and res.returncode == 0:
            return url
    sys.exit("check24_query.py produced no URL — is config/check24-profile.json set up?")


def fetch_html(url: str, attempts: int = 5) -> str:
    """GET the result page, retrying truncated bodies.

    CHECK24 cuts large responses mid-transfer under load — the 4.4 MB result page failed
    this way on a first attempt — and ignores Range, so a cut-off body can only be
    re-fetched whole, never resumed.
    """
    last = ""
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=90) as resp:
                return resp.read().decode("utf-8", "ignore")
        except (urllib.error.URLError, TimeoutError,
                http.client.IncompleteRead) as e:
            last = str(e)
            if attempt + 1 < attempts:
                time.sleep(2.0 * (attempt + 1))
    sys.exit(f"could not fetch the result page after {attempts} attempts: {last}")


def _text(pattern: str, block: str) -> str | None:
    m = re.search(pattern, block, re.S)
    return re.sub(r"\s+", " ", unescape(m.group(1))).strip() if m else None


def _bullet(label: str, block: str) -> str | None:
    """Value of a labelled tariff bullet. The label also occurs inside its own tooltip
    text, so the match is anchored on the bullet_value that follows it."""
    return _text(label + r'.{0,3000}?bullet_value">([^<]*)<', block)


def _grade(block: str) -> str | None:
    """Tarifnote in the scraper's format: always one decimal, comma separator.
    The attribute carries '1' where the rendered page shows '1,0'."""
    raw = _text(r'data-grade="([\d,\.]+)"', block)
    if raw is None:
        return None
    try:
        return f"{float(raw.replace(',', '.')):.1f}".replace(".", ",")
    except ValueError:
        return None


def _wartezeit_per_modul(block: str) -> dict[str, str] | None:
    pairs = re.findall(
        r'tooltip_latency_module__module">([^<]*)</span>\s*'
        r'<span class="tooltip_latency_module__value">([^<]*)<', block)
    out = {re.sub(r"[:\s]+$", "", unescape(k).strip()): unescape(v).strip()
           for k, v in pairs}
    return out or None


def parse_rows(html: str) -> list[dict]:
    """Extract one row per tariff from the server-rendered list."""
    rows: list[dict] = []
    for block in ROW_SPLIT.split(html)[1:]:
        insurer = _text(r'class="provider_logo"[^>]*alt="([^"]*)"', block)
        product = _text(r'class="tariff_name\s*"[^>]*>\s*(.*?)\s*</div>', block)
        # Annual amount; the monthly figure is only in split-up display markup.
        yearly = _text(r'data-tariff-price="([\d\.]+)"', block)
        if not (insurer and product and yearly):
            continue
        rows.append({
            # Rank in the server-rendered order. The hydrated page re-sorts, so this
            # is NOT comparable with a fetch_ratings.py position — snapshots key on
            # insurer|product|selbstbeteiligung, so nothing downstream depends on it.
            "position": len(rows) + 1,
            "insurer": insurer,
            "product": product,
            "tarifnote": _grade(block),
            "bewertung": None,        # Vue-only; filled from the previous snapshot
            "bewertung_anzahl": None,
            "monatlich_eur": round(float(yearly) / 12, 2),
            "selbstbeteiligung": _bullet("Selbstbeteiligung", block),
            "deckungssumme": _bullet("Deckungssumme", block),
            "wartezeit": _bullet("Wartezeit", block),
            "wartezeit_per_modul": _wartezeit_per_modul(block),
        })
    return rows


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s if s is not None else "")
                  .replace(" ", " ").replace(" ", " ")).strip()


def latest_snapshot() -> tuple[Path, dict] | None:
    files = sorted(SNAPDIR.glob("*.json"))
    if not files:
        return None
    try:
        return files[-1], json.loads(files[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def carry_ratings(rows: list[dict]) -> tuple[int, str | None]:
    """Copy customer ratings from the newest snapshot onto the freshly parsed rows.

    Keyed exactly like snapshot.py (insurer|product|selbstbeteiligung) so a row that
    changed price still finds its rating. Returns (rows filled, source date)."""
    prev = latest_snapshot()
    if prev is None:
        return 0, None
    _, snap = prev
    date = snap.get("date")
    by_key = {}
    for t in snap.get("tariffs", []):
        key = "|".join(_norm(t.get(k)) for k in ("insurer", "product", "selbstbeteiligung"))
        by_key[key] = t
    filled = 0
    for r in rows:
        key = "|".join(_norm(r.get(k)) for k in ("insurer", "product", "selbstbeteiligung"))
        old = by_key.get(key)
        if old is None or old.get("bewertung") is None:
            continue
        r["bewertung"] = old.get("bewertung")
        r["bewertung_anzahl"] = old.get("bewertung_anzahl")
        # Provenance, so a carried-over rating is never mistaken for a fresh one.
        r["bewertung_stand"] = old.get("bewertung_stand") or date
        filled += 1
    return filled, date


def compare(rows: list[dict]) -> None:
    """Diff the parsed rows against the newest snapshot without writing anything."""
    prev = latest_snapshot()
    if prev is None:
        print("No snapshot to compare against.")
        return
    path, snap = prev
    def key(t):
        return "|".join(_norm(t.get(k)) for k in ("insurer", "product", "selbstbeteiligung"))
    old = {key(t): t for t in snap.get("tariffs", [])}
    new = {key(t): t for t in rows}
    common = old.keys() & new.keys()
    moved = [(k, old[k].get("monatlich_eur"), new[k].get("monatlich_eur"))
             for k in common
             if old[k].get("monatlich_eur") != new[k].get("monatlich_eur")]
    print(f"vs {path.name}: {len(common)} common, {len(new) - len(common)} new, "
          f"{len(old) - len(common)} gone, {len(moved)} price change(s)")
    for k, o, n in sorted(moved)[:20]:
        print(f"  {k[:70]}: {o} -> {n}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Fetch the CHECK24 result list over plain HTTP (no browser).")
    ap.add_argument("--date", default=str(datetime.date.today()),
                    help="snapshot date label (default: today)")
    ap.add_argument("--snapshot", action="store_true",
                    help="call snapshot.py after saving rows")
    ap.add_argument("--out", metavar="PATH",
                    help="output JSON path (default: tmp/rows_DATE.json)")
    ap.add_argument("--compare", action="store_true",
                    help="parse and diff against the newest snapshot, write nothing")
    ap.add_argument("--html", metavar="PATH",
                    help="parse this saved HTML instead of fetching (offline testing)")
    args = ap.parse_args()

    # flow=panel verticals (hausrat, phv) render their result list entirely
    # client-side — the SSR HTML carries ZERO tariff cards (measured 2026-08-28:
    # 3.4 MB of HTML, 0 result_tile markers), so a browserless price scan is
    # structurally impossible there, not merely flaky. Refuse with the actual
    # alternative instead of retrying into IncompleteRead noise.
    if (_vertical.vertical_config().get("harvest") or {}).get("flow") == "panel":
        sys.exit(f"vertical {_vertical.active()!r} serves no SSR tariff list — "
                 "prices need the browser scan: scripts/fetch_ratings.py --snapshot")

    if args.html:
        html = Path(args.html).read_text(encoding="utf-8", errors="ignore")
    else:
        t0 = time.time()
        html = fetch_html(build_url())
        print(f"Fetched {len(html) // 1024} KiB in {time.time() - t0:.1f}s (no browser)")

    rows = parse_rows(html)
    if not rows:
        sys.exit("No rows parsed — CHECK24 likely changed the result-list markup. "
                 "Compare against scripts/check24_scrape.js and adjust the patterns.")
    print(f"Parsed {len(rows)} tariff(s)")

    if args.compare:
        compare(rows)
        return 0

    filled, src = carry_ratings(rows)
    if filled:
        print(f"Carried {filled}/{len(rows)} customer rating(s) forward from {src} "
              f"(marked bewertung_stand) — run fetch_ratings.py to refresh them")
    else:
        print("No ratings carried forward — bewertung stays null "
              "(run scripts/fetch_ratings.py for a full scan)")

    tmp = _vertical.TMP
    tmp.mkdir(exist_ok=True)
    out = Path(args.out) if args.out else tmp / f"rows_{args.date}.json"
    out.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    shown = out.relative_to(ROOT) if out.is_absolute() and out.is_relative_to(ROOT) else out
    print(f"Saved -> {shown}")

    if args.snapshot:
        for argv in ([sys.executable, str(ROOT / "scripts" / "snapshot.py")],
                     ["uv", "run", str(ROOT / "scripts" / "snapshot.py")]):
            try:
                res = subprocess.run([*argv, str(out), "--date", args.date,
                                      "--source", "fetch_prices (SSR, no browser)"],
                                     cwd=ROOT)
            except OSError:
                continue
            return res.returncode
        sys.exit("could not run snapshot.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
