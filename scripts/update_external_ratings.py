#!/usr/bin/env python3
"""update_external_ratings — assisted refresh for data/sources/external-ratings.json.

The external verdicts are editorial claims a machine cannot re-derive, so this
does NOT scrape verdict text. What it automates is the mechanical part of the
curation loop that check_external_ratings.py only warns about:

  1. fetch each source page once and parse its CURRENT editorial revision date
     (newest visible "Stand: <date>"; JSON-LD dateModified as cross-check),
  2. verify each entry's `evidence` tokens — curator-chosen strings that must
     appear on the page for the recorded verdict to still hold — against the
     tag-stripped page text,
  3. with --apply, bump `stand` to the page date ONLY where the evidence is
     intact; anything else is listed as REVIEW and never touched.

An entry without an `evidence` list falls back to a stem-derived token (weak —
add real evidence strings when curating). Like the checker, only finanztip.de
URLs are in scope by default (F&B is a JS app, test.de prints no stand);
--all overrides.

Run:  python3 scripts/update_external_ratings.py [--all] [--apply]
Exit: 0 = in sync (or all pending updates applied), 1 = review needed or
      pending updates not applied (dry run), 2 = fetch/parse failure.

Needs outbound network to the source hosts — run from a normal terminal, not
from inside a host-allowlisted sandbox.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_external_ratings as cer  # noqa: E402 — fetch_page/GERMAN_MONTHS/RATINGS_PATH
from _jsonio import atomic_write_json  # noqa: E402

MONTH_NUM = {m.lower(): i + 1 for i, m in enumerate(cer.GERMAN_MONTHS)}

_STAND_RE = re.compile(
    r"Stand[:\s]{1,3}(\d{1,2})\.\s*([A-Za-zÄÖÜäöü]+)\s+(\d{4})"   # 20. August 2025
    r"|Stand[:\s]{1,3}(\d{1,2})\.(\d{1,2})\.(\d{4})"              # 20.08.2025
    r"|Stand[:\s]{1,3}(\d{4})-(\d{2})-(\d{2})"                    # 2025-08-20
)
_DATE_MODIFIED_RE = re.compile(r'"dateModified"\s*:\s*"(\d{4}-\d{2}-\d{2})')


def to_plain(page: str) -> str:
    """Tag-stripped, entity-decoded, whitespace-collapsed page text — evidence
    tokens are prose fragments and must not break on markup inside a sentence."""
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", page,
                  flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text))


def page_stand(plain: str) -> str | None:
    """Newest visible 'Stand: <date>' as ISO — the editorial revision date the
    recorded `stand` fields refer to. Pages print older stands in embedded
    boxes; max() picks the main article's."""
    dates: list[dt.date] = []
    for m in _STAND_RE.finditer(plain):
        try:
            if m.group(1):
                month = MONTH_NUM.get(m.group(2).lower())
                if month:
                    dates.append(dt.date(int(m.group(3)), month, int(m.group(1))))
            elif m.group(4):
                dates.append(dt.date(int(m.group(6)), int(m.group(5)), int(m.group(4))))
            else:
                dates.append(dt.date(int(m.group(7)), int(m.group(8)), int(m.group(9))))
        except ValueError:
            continue
    return max(dates).isoformat() if dates else None


def _iter_subjects(data: dict):
    """Yield (block, subject, entry) for every rating entry, keeping the key
    that _iter_entries in the checker throws away."""
    for block in ("tariffs", "insurers"):
        table = data.get(block)
        if not isinstance(table, dict):
            continue
        for key, entries in table.items():
            if isinstance(entries, list):
                for e in entries:
                    if isinstance(e, dict):
                        yield block, key, e
    for n in data.get("_market_notes") or []:
        if isinstance(n, dict):
            yield "_market_notes", f"{n.get('versicherer', '?')}/{n.get('tarif', '?')}", n


def evidence_for(block: str, subject: str, entry: dict) -> tuple[list[str], bool]:
    """(tokens, curated) — curated=False means the weak stem-derived fallback."""
    ev = entry.get("evidence")
    if isinstance(ev, list) and ev:
        return [str(t) for t in ev], True
    if block == "tariffs":
        return [subject.split("__", 1)[0].replace("-", " ")], False
    return [subject.split("/", 1)[0].replace("-", " ")], False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--all", action="store_true",
                    help="process every source URL, not just finanztip.de")
    ap.add_argument("--apply", action="store_true",
                    help="write bumped stand dates back (default: dry run)")
    args = ap.parse_args()

    try:
        data = json.loads(cer.RATINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"ERROR: cannot read {cer.RATINGS_PATH}: {e}", file=sys.stderr)
        return 2

    subjects = [(b, k, e) for b, k, e in _iter_subjects(data)
                if e.get("url") and e.get("stand")
                and (args.all or "finanztip.de" in e["url"])]
    if not subjects:
        print("nothing to do (no matching source URLs with a stand date)")
        return 0

    in_sync = updated = review = fetch_err = 0
    for url in sorted({e["url"] for _, _, e in subjects}):
        print(f"==> {url}")
        try:
            page = cer.fetch_page(url)
        except OSError as e:
            print(f"    FETCH FAILED ({e}) — check network/URL by hand")
            fetch_err += 1
            continue
        plain = to_plain(page)
        pstand = page_stand(plain)
        dmod = _DATE_MODIFIED_RE.search(page)
        print(f"    page stand: {pstand or '(none found)'}"
              + (f"  (dateModified: {dmod.group(1)})" if dmod else ""))
        if pstand is None:
            print("    cannot determine the page's stand — review by hand")
            fetch_err += 1
            continue

        for block, subject, entry in subjects:
            if entry["url"] != url:
                continue
            tokens, curated = evidence_for(block, subject, entry)
            missing = [t for t in tokens if t.casefold() not in plain.casefold()]
            tag = "" if curated else " (weak fallback evidence — curate an 'evidence' list)"
            name = f"{block}/{subject}"
            if missing:
                # Evidence gone -> the verdict may no longer hold. Never bump.
                print(f"    REVIEW  {name}: evidence missing: "
                      + ", ".join(repr(t) for t in missing)
                      + " — verify the verdict on the page and re-curate")
                review += 1
            elif entry["stand"] == pstand:
                print(f"    ok      {name}: stand {pstand} current, evidence intact{tag}")
                in_sync += 1
            elif entry["stand"] < pstand:
                verb = "UPDATED" if args.apply else "PENDING"
                print(f"    {verb} {name}: stand {entry['stand']} -> {pstand} "
                      f"(evidence intact{tag})")
                if args.apply:
                    entry["stand"] = pstand
                updated += 1
            else:
                print(f"    REVIEW  {name}: recorded stand {entry['stand']} is NEWER "
                      f"than the page's {pstand} — check the entry")
                review += 1

    if args.apply and updated:
        atomic_write_json(cer.RATINGS_PATH, data)
        print(f"wrote {cer.RATINGS_PATH.relative_to(cer.REPO_ROOT)}")

    pending = 0 if args.apply else updated
    print(f"summary: {in_sync} in sync, {updated} "
          + ("updated" if args.apply else "pending update")
          + f", {review} need review, {fetch_err} fetch/parse failure(s)")
    if fetch_err:
        return 2
    return 1 if (review or pending) else 0


if __name__ == "__main__":
    raise SystemExit(main())
