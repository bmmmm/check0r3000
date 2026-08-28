#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["jsonschema>=4"]
# ///
"""Post-update feedback: one Markdown report + one append-only run-log line.

Called at the end of the two full-update funnels (update-all.sh and the TUI's
[U]); also runnable standalone after any pipeline.sh cycle. Reuses the existing
building blocks instead of re-deriving anything: regression.py (golden + market
checks, hence the jsonschema dep), tui_data.load_change_summary() (feature+price
diffs), price_history.market_stats(), and the tmp/ sidecars written by
extract.py (extract-cost.json) and golden_pins.py (golden-pin-repairs.json).

Writes:  tmp/update-report.md      (overwritten per run — the human summary)
         tmp/update-runs.jsonl     (append-only — the durable, queryable log)
Exit:    always 0 — a report bug must never fail the pipeline it reports on.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import price_history  # noqa: E402
import regression as regr  # noqa: E402
import tui_data  # noqa: E402
import _vertical  # noqa: E402
from _jsonio import load_json_or  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TMP = _vertical.TMP
REPORT_MD = TMP / "update-report.md"
RUN_LOG = TMP / "update-runs.jsonl"
COST_SIDECAR = TMP / "extract-cost.json"
REPAIRS_SIDECAR = TMP / "golden-pin-repairs.json"


def golden_status(schema: dict) -> tuple[int, int, dict[str, list[str]]]:
    """(passed, total, failures) over the golden-pinned stems."""
    golden_doc = load_json_or(regr.GOLDEN, {}) or {}
    fails: dict[str, list[str]] = {}
    total = 0
    for stem, entry in sorted(golden_doc.get("tariffs", {}).items()):
        total += 1
        rec = load_json_or(regr.TARIFFS / f"{stem}.json", None)
        if not isinstance(rec, dict):
            fails[stem] = ["record missing/unreadable"]
            continue
        violations = regr.check_record(rec, entry, schema)
        if violations:
            fails[stem] = violations
    return total - len(fails), total, fails


def market_status(schema: dict) -> tuple[int, int, dict[str, list[str]]]:
    """(passed, total, failures) of the market-wide sweep over out/tariffs/."""
    fails: dict[str, list[str]] = {}
    paths = sorted(regr.TARIFFS.glob("*.json"))
    for path in paths:
        rec = load_json_or(path, None)
        if not isinstance(rec, dict):
            fails[path.stem] = ["record missing/unreadable"]
            continue
        violations = regr.check_market_record(rec, schema)
        if violations:
            fails[path.stem] = violations
    return len(paths) - len(fails), len(paths), fails


def _fmt_price(value) -> str:
    return f"{value:.2f} €" if isinstance(value, (int, float)) else "—"


def _feature_diff_line(diff: dict) -> str:
    """One compact German line for a diff_features() dict."""
    bits: list[str] = []
    if diff.get("modules"):
        bits.append(f"{len(diff['modules'])} Modul(e)")
    if diff.get("coverage"):
        fields = ", ".join(c["field"] for c in diff["coverage"][:4])
        bits.append(f"Coverage: {fields}")
    for field, label in (("leistungen", "Leistungen"), ("ausschluesse", "Ausschlüsse"),
                         ("besonderheiten", "Besonderheiten")):
        d = diff.get(field)
        if d:
            bits.append(f"{label} +{len(d['added'])}/−{len(d['removed'])}")
    return "; ".join(bits) or "Detailänderung"


def build(label: str) -> tuple[str, dict]:
    """Assemble (markdown report, run-log entry)."""
    now = datetime.datetime.now()
    today = now.date().isoformat()

    schema = json.loads(regr.SCHEMA.read_text(encoding="utf-8"))
    cost = load_json_or(COST_SIDECAR, {}) or {}
    repairs = load_json_or(REPAIRS_SIDECAR, []) or []
    g_pass, g_total, g_fails = golden_status(schema)
    m_pass, m_total, m_fails = market_status(schema)
    changes = tui_data.load_change_summary()
    stats = price_history.market_stats()

    # Day-granularity filter (inherited from feature/price history): only what
    # landed today counts as "this run's" changes.
    feat_today = {
        stem: [(old, new, diff) for old, new, diff in ci.feature_changelog
               if new == today]
        for stem, ci in changes.items()
    }
    feat_today = {s: v for s, v in feat_today.items() if v}
    price_today = {
        stem: [e for e in ci.price_changelog if e.get("date") == today]
        for stem, ci in changes.items()
    }
    price_today = {s: v for s, v in price_today.items() if v}

    cost_stale = bool(cost) and str(cost.get("ts", ""))[:10] != today

    lines: list[str] = []
    lines.append(f"# Update-Report — {now.isoformat(timespec='seconds')}")
    lines.append("")
    lines.append(f"Lauf: `{label}`")
    lines.append("")

    lines.append("## Kosten & Extraktion")
    lines.append("")
    if not cost:
        lines.append("Keine Kostendaten — extract.py ist in diesem Zyklus nicht gelaufen.")
    else:
        if cost_stale:
            lines.append(f"⚠ Kostendaten sind vom {str(cost.get('ts', ''))[:10]} — "
                         f"extract.py lief heute nicht (oder brach vor dem Schreiben ab).")
        lines.append(f"- Modell: `{cost.get('model')}` · Filter: `{cost.get('filter')}` "
                     f"· Repeat: {cost.get('repeat')}")
        lines.append(f"- Tarife: {cost.get('extracted', 0)} extrahiert · "
                     f"{cost.get('cached', 0)} cached · {cost.get('shared', 0)} shared · "
                     f"{cost.get('failed', 0)} fehlgeschlagen")
        lines.append(f"- Kosten: {cost.get('cost_usd', 0.0):.4f} USD "
                     f"({cost.get('priced_calls', 0)} bepreiste / "
                     f"{cost.get('unpriced_calls', 0)} unbepreiste Calls)")
    lines.append("")

    lines.append("## Golden-Pin-Reparaturen")
    lines.append("")
    if not repairs:
        lines.append("Keine — kein gepinntes Feld wurde von der Extraktion verletzt.")
    else:
        for r in repairs:
            lines.append(f"- `{r.get('stem')}`: `{r.get('path')}` war "
                         f"`{r.get('old_value')!r}` → null")
    lines.append("")

    lines.append("## Regression")
    lines.append("")
    lines.append(f"- Golden-Stems: **{g_pass}/{g_total}** bestanden")
    lines.append(f"- Markt-Sweep (Schema + beitrag-null): **{m_pass}/{m_total}** bestanden")
    for title, fails in (("Golden", g_fails), ("Markt", m_fails)):
        for stem, violations in sorted(fails.items()):
            lines.append(f"- ✗ {title} `{stem}`:")
            for v in violations[:3]:
                lines.append(f"  - {v}")
            if len(violations) > 3:
                lines.append(f"  - … {len(violations) - 3} weitere")
    lines.append("")

    lines.append(f"## Änderungen heute ({today})")
    lines.append("")
    if not feat_today and not price_today:
        lines.append("Keine Leistungs- oder Preisänderungen registriert.")
    for stem, entries in sorted(feat_today.items()):
        for _old, _new, diff in entries:
            lines.append(f"- `{stem}` — Leistungen: {_feature_diff_line(diff)}")
    for stem, entries in sorted(price_today.items()):
        for e in entries:
            arrow = "↑" if e.get("delta", 0) > 0 else "↓"
            lines.append(f"- `{stem}` — Preis: {_fmt_price(e.get('old_price'))} → "
                         f"{_fmt_price(e.get('new_price'))} ({arrow}"
                         f"{abs(e.get('delta', 0)):.2f} €)")
    lines.append("")

    lines.append("## Markt-Puls")
    lines.append("")
    if not stats:
        lines.append("Keine Snapshots vorhanden.")
    else:
        cur = stats[-1]
        lines.append(f"- Snapshot {cur['date']}: {cur['count']} Tarife "
                     f"({cur['priced']} bepreist) · min {_fmt_price(cur['min'])} · "
                     f"median {_fmt_price(cur['median'])} · max {_fmt_price(cur['max'])}")
        if len(stats) >= 2:
            prev = stats[-2]
            dcount = cur["count"] - prev["count"]
            lines.append(f"- vs. {prev['date']}: {dcount:+d} Tarife, "
                         f"median {_fmt_price(prev['median'])} → {_fmt_price(cur['median'])}")
    lines.append("")

    entry = {
        "ts": now.isoformat(timespec="seconds"),
        "label": label,
        "model": cost.get("model"),
        "filter": cost.get("filter"),
        "repeat": cost.get("repeat"),
        "cost_usd": cost.get("cost_usd"),
        "cost_stale": cost_stale,
        "extracted": cost.get("extracted"),
        "cached": cost.get("cached"),
        "shared": cost.get("shared"),
        "failed": cost.get("failed"),
        "golden_repairs": len(repairs),
        "golden_pass": g_pass,
        "golden_total": g_total,
        "market_pass": m_pass,
        "market_total": m_total,
        "feature_changes_today": sum(len(v) for v in feat_today.values()),
        "price_changes_today": sum(len(v) for v in price_today.values()),
    }
    return "\n".join(lines), entry


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="manual",
                    help="run label recorded in the report + run log "
                         "(e.g. update-all, tui-update-all)")
    args = ap.parse_args()
    try:
        report, entry = build(args.label)
        TMP.mkdir(parents=True, exist_ok=True)
        REPORT_MD.write_text(report + "\n", encoding="utf-8")
        # Append-only by design: JSONL accumulates across runs, so no
        # atomic-replace here — worst case a crash truncates only the last line.
        with RUN_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        print(f"update_report: wrote {REPORT_MD.relative_to(ROOT)} "
              f"(+1 line {RUN_LOG.relative_to(ROOT)})")
    except Exception:  # noqa: BLE001 — a report bug must never fail the pipeline
        traceback.print_exc()
        print("update_report: FAILED (non-fatal) — see traceback above", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
