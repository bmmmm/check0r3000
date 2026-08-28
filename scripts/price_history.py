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

import _vertical

ROOT = _vertical.ROOT
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def load_all_price_history(resolve_stem_fn) -> dict[str, list[dict]]:
    """Build per-stem price series from all snapshots.

    resolve_stem_fn(insurer, product) -> str | None  (tui_data.resolve_stem)

    Returns dict[stem, [{"date": str, "price": float | None}, ...]] oldest→newest,
    one entry per snapshot the stem appeared in.

    The series is pinned to ONE Selbstbeteiligung band per stem — the band present
    in the most snapshots, cheapest as tie-break — so consecutive prices are always
    compared like-for-like. Taking the cheapest variant per snapshot instead (the
    old behaviour) reports a PHANTOM price change whenever the set of scraped SB
    bands shifts between snapshots: if the previously-cheapest band drops out, the
    min jumps to the next band and looks like a hike though no band actually moved.
    Snapshots where the pinned band is absent become price=None (a gap, skipped by
    price_changelog) rather than a fabricated jump. Trade-off: a price change
    isolated to a non-pinned band is not surfaced — acceptable because a real rate
    change moves all bands together, so the pinned band reflects it too.
    """
    snapshot_dir = _vertical.snapshots_dir()
    if not snapshot_dir.is_dir():
        return {}

    snap_files = sorted(
        p for p in snapshot_dir.glob("*.json") if _DATE_RE.match(p.stem)
    )

    # stem -> date -> sb_band -> price  (price-bearing variants only)
    prices: dict[str, dict[str, dict[str, float]]] = {}
    # stem -> set of dates it appeared in at all (priced or not), so the emitted
    # series spans exactly the snapshots the stem was present in.
    appeared: dict[str, set[str]] = {}

    for snap_path in snap_files:
        date = snap_path.stem
        try:
            data = json.loads(snap_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for t in data.get("tariffs", []):
            ins = t.get("insurer", "")
            prod = t.get("product", "")
            stem = resolve_stem_fn(ins, prod)
            if not stem:
                continue
            appeared.setdefault(stem, set()).add(date)
            price = t.get("monatlich_eur")
            if isinstance(price, bool) or not isinstance(price, (int, float)):
                continue
            band = (t.get("selbstbeteiligung") or "").strip()
            day = prices.setdefault(stem, {}).setdefault(date, {})
            # A snapshot may list the same (stem, SB) twice (snapshot.py permits
            # duplicate keys); keep the cheapest so each band has one price per date.
            if band not in day or price < day[band]:
                day[band] = price

    result: dict[str, list[dict]] = {}
    for stem, dates_set in appeared.items():
        dates_seen = sorted(dates_set)
        # Invert to band -> {date: price} to find the band with the widest coverage.
        band_dates: dict[str, dict[str, float]] = {}
        for date in dates_seen:
            for band, price in prices.get(stem, {}).get(date, {}).items():
                band_dates.setdefault(band, {})[date] = price
        if not band_dates:
            # Stem seen but never priced — preserve it with a None series.
            result[stem] = [{"date": d, "price": None} for d in dates_seen]
            continue
        # Pin: most snapshots first (kills the phantom), cheapest as tie-break
        # (keeps the representative-low intent), band string last for determinism.
        _, pinned = min(
            band_dates.items(),
            key=lambda kv: (-len(kv[1]), min(kv[1].values()), kv[0]),
        )
        result[stem] = [{"date": d, "price": pinned.get(d)} for d in dates_seen]

    return result


def market_stats() -> list[dict]:
    """Per-snapshot market aggregates, oldest→newest — the Verlauf time-series view.

    Returns [{date, count, priced, min, median, max}, ...] over ALL rows of each
    snapshot (no stem resolution: this is the whole scraped market, including
    tariffs that never map to a manifest stem). Unreadable snapshots are skipped,
    price-less rows count toward `count` but not the price aggregates.
    """
    import statistics

    snapshot_dir = _vertical.snapshots_dir()
    if not snapshot_dir.is_dir():
        return []
    out: list[dict] = []
    for snap_path in sorted(
        p for p in snapshot_dir.glob("*.json") if _DATE_RE.match(p.stem)
    ):
        try:
            data = json.loads(snap_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        tariffs = data.get("tariffs", [])
        prices = [
            t["monatlich_eur"] for t in tariffs
            if isinstance(t.get("monatlich_eur"), (int, float))
            and not isinstance(t.get("monatlich_eur"), bool)
        ]
        out.append({
            "date": snap_path.stem,
            "count": len(tariffs),
            "priced": len(prices),
            "min": min(prices) if prices else None,
            "median": statistics.median(prices) if prices else None,
            "max": max(prices) if prices else None,
        })
    return out


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
    ap.add_argument("--market", action="store_true",
                    help="show per-snapshot market aggregates instead of per-stem series")
    args = ap.parse_args()

    if args.market:
        stats = market_stats()
        if not stats:
            print("No snapshots found in data/snapshots/.")
            raise SystemExit(0)
        print(f"{'date':<12} {'tariffs':>7} {'priced':>6} {'min':>8} {'median':>8} {'max':>8}")
        for s in stats:
            fmt = lambda v: f"{v:8.2f}" if v is not None else f"{'—':>8}"
            print(f"{s['date']:<12} {s['count']:>7} {s['priced']:>6} "
                  f"{fmt(s['min'])} {fmt(s['median'])} {fmt(s['max'])}")
        raise SystemExit(0)

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
