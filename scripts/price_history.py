#!/usr/bin/env python3
"""price_history — Track market price changes per stem across all snapshots.

Reads all data/snapshots/*.json and computes a per-stem price series.
Pure read-only, no disk writes. Standalone runnable for spot-checks.

Run:  python3 scripts/price_history.py [--stem <stem>]
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_DIR = ROOT / "data" / "snapshots"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def load_all_price_history(resolve_stem_fn) -> dict[str, list[dict]]:
    """Build per-stem price series from all snapshots.

    resolve_stem_fn(insurer, product) -> str | None  (tui_data.resolve_stem)

    Returns dict[stem, [{"date": str, "price": float | None}, ...]] oldest→newest.
    Each snapshot entry uses the lowest priced variant seen for that stem
    (representative price across SB bands).
    """
    if not SNAPSHOT_DIR.is_dir():
        return {}

    snap_files = sorted(
        p for p in SNAPSHOT_DIR.glob("*.json") if _DATE_RE.match(p.stem)
    )

    by_stem: dict[str, dict[str, float | None]] = {}

    for snap_path in snap_files:
        date = snap_path.stem
        try:
            data = json.loads(snap_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        snap_prices: dict[str, float | None] = {}
        for t in data.get("tariffs", []):
            ins = t.get("insurer", "")
            prod = t.get("product", "")
            stem = resolve_stem_fn(ins, prod)
            if not stem:
                continue
            price = t.get("monatlich_eur")
            if price is None:
                snap_prices.setdefault(stem, None)
            elif stem not in snap_prices or snap_prices[stem] is None or price < snap_prices[stem]:
                snap_prices[stem] = price
        for stem, price in snap_prices.items():
            by_stem.setdefault(stem, {})[date] = price

    return {
        stem: [{"date": d, "price": p} for d, p in sorted(dp.items())]
        for stem, dp in by_stem.items()
    }


def price_changelog(series: list[dict]) -> list[dict]:
    """Return [{date, old_price, new_price, delta}, ...] for each price change event."""
    changes = []
    priced = [e for e in series if e["price"] is not None]
    for prev, curr in zip(priced, priced[1:]):
        if abs(prev["price"] - curr["price"]) > 0.005:
            changes.append({
                "date": curr["date"],
                "old_price": prev["price"],
                "new_price": curr["price"],
                "delta": curr["price"] - prev["price"],
            })
    return changes


def price_change_count(series: list[dict]) -> int:
    return len(price_changelog(series))


def last_price_change_date(series: list[dict]) -> str | None:
    changes = price_changelog(series)
    return changes[-1]["date"] if changes else None


if __name__ == "__main__":
    import argparse

    sys.path.insert(0, str(ROOT / "scripts"))
    from tui_data import resolve_stem  # noqa: E402

    ap = argparse.ArgumentParser(description="Show snapshot-based price history per stem")
    ap.add_argument("--stem", help="Show only this stem")
    args = ap.parse_args()

    history = load_all_price_history(resolve_stem)
    stems = [args.stem] if args.stem else sorted(history)
    for stem in stems:
        series = history.get(stem, [])
        changes = price_changelog(series)
        n = price_change_count(series)
        print(f"\n{stem}  ({len(series)} snapshots, {n} price changes)")
        for e in series:
            p = f"{e['price']:.2f}" if e["price"] is not None else "—"
            print(f"  {e['date']}  €{p}")
        if changes:
            print("  Changes:")
            for ch in changes:
                sign = "+" if ch["delta"] > 0 else ""
                print(f"    {ch['date']}: {ch['old_price']:.2f} → {ch['new_price']:.2f}  ({sign}{ch['delta']:.2f})")
