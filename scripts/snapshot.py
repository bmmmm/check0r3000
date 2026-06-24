#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Snapshot the CHECK24 market scan and diff snapshots over time.

The point: keep a dated record of the whole result list (price, Tarifnote,
Selbstbeteiligung per tariff) so a later run can show what CHANGED — price moves,
tariffs added or pulled. This is the lightweight "did the market shift?" database;
no engine, just dated JSON files plus a diff. The snapshots reflect a personal quote
profile, so they are gitignored (data/snapshots/).

Build a snapshot from scraped rows (scripts/check24_scrape.js -> window.check24Rows,
saved as JSON; or the pipe-separated harvest file):
    uv run scripts/snapshot.py rows.json --date 2026-06-24 --label "couple, Bonn"
    uv run scripts/snapshot.py rows.psv  --date 2026-06-24

Diff two snapshots (old -> new):
    uv run scripts/snapshot.py --diff data/snapshots/2026-06-24.json data/snapshots/2026-09-01.json
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAPDIR = ROOT / "data" / "snapshots"
# tarifnote = CHECK24 expert grade; bewertung/_anzahl = customer rating (stars + count).
# The PSV path carries only the first six; the JSON scrape may add the rating fields
# (null where absent).
FIELDS = ("position", "insurer", "product", "tarifnote", "monatlich_eur",
          "selbstbeteiligung", "bewertung", "bewertung_anzahl")


def _eur(s) -> float | None:
    """Parse a German price token to a float. Mirrors check24_scrape.js eur(): drops
    the currency symbol / NBSP and German formatting (dot thousands, comma decimal),
    so a hand-authored PSV price like '12,90 €' or '1.234,56' parses instead of
    silently becoming None. A plain number passes through unchanged."""
    if isinstance(s, (int, float)) and not isinstance(s, bool):
        return float(s)
    s = str(s) if s is not None else ""
    # Keep only digits and the two separators; this also drops the
    # currency symbol, NBSP, narrow-NBSP and plain spaces in one shot.
    s = re.sub(r"[^\d.,]", "", s)
    if not s:
        return None
    try:
        return float(s.replace(".", "").replace(",", "."))
    except ValueError:
        return None


def _norm_key(s) -> str:
    """Collapse NBSP / narrow-NBSP and whitespace runs for a stable snapshot key.

    The JS scrape may leave a non-breaking space (CHECK24 uses it around prices and
    Selbstbeteiligung) while the PSV path carries plain spaces. Without normalising,
    the same tariff keys differently across the two inputs and a diff shows a phantom
    add+remove for it."""
    if s is None:
        return ""
    return " ".join(str(s).replace("\u00a0", " ").replace("\u202f", " ").split())


def load_rows(path: Path) -> list[dict]:
    """Accept either a JSON array (window.check24Rows) or a pipe-separated harvest
    (position|insurer|product|tarifnote|monatlich_eur|selbstbeteiligung, '#' = comment)."""
    if not path.is_file():
        sys.exit(f"Rows file not found: {path}")
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            sys.exit(f"{path} is not a JSON array of rows.")
        bad = [i for i, r in enumerate(data) if not isinstance(r, dict)]
        if bad:
            sys.exit(f"{path}: row(s) at index {bad[:5]} are not JSON objects "
                     f"(each row must be {{position, insurer, ...}}).")
        rows = data
    else:
        rows = []
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            if len(parts) != 6:
                sys.exit(f"{path}:{n}: expected 6 '|'-separated fields, got {len(parts)}.")
            pos, insurer, product, note, price, sb = (p.strip() for p in parts)
            rows.append({"position": int(pos) if pos.isdigit() else None,
                         "insurer": insurer, "product": product, "tarifnote": note,
                         "monatlich_eur": _eur(price), "selbstbeteiligung": sb})
    out = []
    for r in rows:
        rec = {k: r.get(k) for k in FIELDS}
        # The PSV path already coerces the price via _eur; a JSON scrape may carry it
        # as a string ("12,90"). Normalise both paths to float|None so diff arithmetic
        # (abs(o - n)) can never hit a string and crash.
        if isinstance(rec.get("monatlich_eur"), str):
            rec["monatlich_eur"] = _eur(rec["monatlich_eur"])
        rec["key"] = "|".join(_norm_key(rec.get(k))
                              for k in ("insurer", "product", "selbstbeteiligung"))
        out.append(rec)
    return out


def build(args) -> int:
    rows = load_rows(Path(args.rows))
    # Validate/normalise the date so the filename is always canonical zero-padded ISO
    # — the prev-snapshot tip below compares names lexicographically.
    if args.date:
        try:
            date = datetime.date.fromisoformat(args.date).isoformat()
        except ValueError:
            sys.exit(f"--date must be ISO YYYY-MM-DD, got {args.date!r}.")
    else:
        date = datetime.date.today().isoformat()
    snap = {"date": date, "profile": args.label or "", "source": args.source,
            "count": len(rows), "tariffs": rows}
    SNAPDIR.mkdir(parents=True, exist_ok=True)
    dest = SNAPDIR / f"{date}.json"
    if dest.exists():
        print(f"note: overwriting existing snapshot {dest.relative_to(ROOT)}.",
              file=sys.stderr)
    # Atomic write: a crash (Ctrl-C, disk full, kill) mid-write must not truncate the
    # only copy of a dated, non-regenerable snapshot. Write a temp twin on the same
    # filesystem, then rename it into place.
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(snap, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(dest)
    dupes = len(rows) - len({r["key"] for r in rows})
    print(f"Wrote {dest.relative_to(ROOT)} — {len(rows)} tariffs"
          + (f" ({dupes} share a key: same product+Selbstbeteiligung)" if dupes else ""))
    prev = sorted(p for p in SNAPDIR.glob("*.json") if p.name < dest.name)
    if prev:
        print(f"Tip: diff against the previous snapshot:\n"
              f"  uv run scripts/snapshot.py --diff {prev[-1].relative_to(ROOT)} {dest.relative_to(ROOT)}")
    return 0


def _by_key(snap_path: Path) -> dict[str, dict]:
    data = json.loads(snap_path.read_text(encoding="utf-8"))
    # Keep the cheapest entry per key, so a duplicated key compares deterministically.
    out: dict[str, dict] = {}
    for t in data.get("tariffs", []):
        # Normalise here too so a snapshot written before the key-normalisation fix
        # (NBSP still in its stored key) compares cleanly against a fresh one.
        k = _norm_key(t.get("key"))
        if k not in out or (t.get("monatlich_eur") or 1e9) < (out[k].get("monatlich_eur") or 1e9):
            out[k] = t
    return out


def diff(args) -> int:
    old_p, new_p = Path(args.diff[0]), Path(args.diff[1])
    for p in (old_p, new_p):
        if not p.is_file():
            sys.exit(f"Snapshot not found: {p}")
    old, new = _by_key(old_p), _by_key(new_p)
    added = [new[k] for k in new if k not in old]
    removed = [old[k] for k in old if k not in new]
    changed = []
    for k in new.keys() & old.keys():
        o, n = old[k].get("monatlich_eur"), new[k].get("monatlich_eur")
        if o is not None and n is not None and abs(o - n) >= 0.01:
            changed.append((k, o, n, n - o))

    print(f"Diff {old_p.name} -> {new_p.name}: "
          f"{len(changed)} price change(s), {len(added)} added, {len(removed)} removed.\n")
    for k, o, n, d in sorted(changed, key=lambda x: -abs(x[3])):
        print(f"  ~ {k}\n      {o:.2f} -> {n:.2f} EUR  ({d:+.2f})")
    for t in sorted(added, key=lambda t: t.get("monatlich_eur") or 0):
        print(f"  + {t['key']}  ({t.get('monatlich_eur')} EUR)")
    for t in removed:
        print(f"  - {t['key']}  (was {t.get('monatlich_eur')} EUR)")
    if not (changed or added or removed):
        print("  (identical)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Snapshot the CHECK24 market scan and diff over time.")
    ap.add_argument("rows", nargs="?", help="scraped rows (.json array or .psv) to snapshot")
    ap.add_argument("--date", help="snapshot date YYYY-MM-DD (default: today)")
    ap.add_argument("--label", help="profile label stored in the snapshot")
    ap.add_argument("--source", default="check24 rsv vergleichsergebnis (all insurers)",
                    help="provenance label")
    ap.add_argument("--diff", nargs=2, metavar=("OLD", "NEW"), help="diff two snapshot files")
    args = ap.parse_args()

    if args.diff:
        return diff(args)
    if not args.rows:
        ap.error("give a rows file to snapshot, or --diff OLD NEW")
    return build(args)


if __name__ == "__main__":
    raise SystemExit(main())
