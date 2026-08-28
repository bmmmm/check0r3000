#!/usr/bin/env python3
"""Move the LOCAL (gitignored) single-vertical artifacts into the vertical namespace.

The tracked files migrate via `git mv` in the layout commit and arrive through a
normal merge/pull. The gitignored artifacts — raw PDFs, extracted texts, snapshots,
personal offers, the PII quote profile, enriched records — exist only on each
machine, so every checkout runs this once after pulling the namespace commit:

    python3 scripts/migrate_layout.py          # dry-run: show what would move
    python3 scripts/migrate_layout.py --apply  # actually move

Idempotent: a source that no longer exists is skipped; an existing non-empty target
is never overwritten (reported instead, resolve by hand). stdlib only.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
V = "rechtsschutz"  # the only vertical that predates the namespace

MOVES: list[tuple[str, str]] = [
    ("data/inbox", f"data/{V}/inbox"),
    ("data/raw", f"data/{V}/raw"),
    ("data/extracted", f"data/{V}/extracted"),
    ("data/snapshots", f"data/{V}/snapshots"),
    ("out/enriched", f"out/{V}/enriched"),
    ("out/screenshots", f"out/{V}/screenshots"),
    ("config/check24-profile.json", f"config/verticals/{V}/check24-profile.json"),
    ("config/favorite-notes.json", f"config/verticals/{V}/favorite-notes.json"),
]
# data/offers: the tracked _example.json/README.md move via git mv; every OTHER file
# there is a personal (gitignored) offer and moves here.
OFFERS_SRC = "data/offers"
OFFERS_DST = f"data/{V}/offers"
OFFERS_TRACKED = {"_example.json", "README.md"}


def plan() -> list[tuple[Path, Path]]:
    out: list[tuple[Path, Path]] = []
    for src_rel, dst_rel in MOVES:
        src = ROOT / src_rel
        if src.exists():
            out.append((src, ROOT / dst_rel))
    offers = ROOT / OFFERS_SRC
    if offers.is_dir():
        for f in sorted(offers.iterdir()):
            if f.name in OFFERS_TRACKED or f.name.startswith("."):
                continue
            out.append((f, ROOT / OFFERS_DST / f.name))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="move (default: dry-run)")
    args = ap.parse_args()

    moves = plan()
    if not moves:
        print("Nothing to migrate — no pre-namespace local artifacts found.")
        return 0

    blocked = 0
    for src, dst in moves:
        rel_src, rel_dst = src.relative_to(ROOT), dst.relative_to(ROOT)
        if dst.exists() and (not dst.is_dir() or any(dst.iterdir())):
            print(f"  BLOCKED  {rel_src} -> {rel_dst} (target exists, resolve by hand)")
            blocked += 1
            continue
        if args.apply:
            if dst.exists():  # empty leftover dir from a partial earlier run
                dst.rmdir()
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
            print(f"  moved    {rel_src} -> {rel_dst}")
        else:
            print(f"  would move  {rel_src} -> {rel_dst}")

    if not args.apply:
        print(f"\nDry-run: {len(moves)} move(s) pending — re-run with --apply.")
    elif blocked:
        print(f"\n{blocked} move(s) blocked — resolve the named targets and re-run.",
              file=sys.stderr)
        return 1
    else:
        print(f"\nDone: {len(moves)} move(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
