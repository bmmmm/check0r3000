#!/usr/bin/env -S uv run --script
"""Scorecard scoring — the single source of truth for the benchmark quality points.

Both the eval CLI (`eval.py --scorecard`) and the TUI Benchmark tab feed the same
aggregated rows through these functions, so the weighting and the scoring rules
live in exactly one place. A "row" is the per-(tariff, model, input) aggregate
written to benchmarks/results.json (keys used here: runs, ok, schema_ok, faithful,
modules_max, unsupported_max). Latency and cost are operational columns and never
fold into the score.

Self-test:  python3 scripts/scorecard.py
"""
from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_JSON = REPO_ROOT / "benchmarks" / "results.json"

# Faithful 50 / Schema 20 / Hallucination-free 15 / Module coverage 15 = 100.
WEIGHTS = {"faithful": 50, "schema": 20, "halluc": 15, "modules": 15}


def score_row(r: dict, max_modules: int) -> dict:
    """Quality scorecard points for one aggregated row (0-100, correctness only).
    Faithful 50 / Schema 20 / Hallucination-free 15 / Module coverage 15. Module
    credit is GATED on the schema-valid rate, so breadth over schema-invalid output
    earns nothing. Zero successful runs = DNF (total 0). Latency and cost stay OUT
    of the score by design — a slow free model must not out-point a faithful one."""
    max_modules = max(1, max_modules)  # guard the denominator on direct calls
    runs = max(1, r["runs"])
    if r["ok"] == 0:
        return {"faithful": 0.0, "schema": 0.0, "halluc": 0.0, "modules": 0.0,
                "total": 0.0, "dnf": True}
    schema_rate = r["schema_ok"] / runs
    faithful = WEIGHTS["faithful"] * (r["faithful"] / runs)
    schema = WEIGHTS["schema"] * schema_rate
    halluc = max(0.0, WEIGHTS["halluc"] - 5 * r["unsupported_max"])
    modules = WEIGHTS["modules"] * (r["modules_max"] / max_modules) * schema_rate
    return {"faithful": faithful, "schema": schema, "halluc": halluc,
            "modules": modules, "total": faithful + schema + halluc + modules,
            "dnf": False}


def scored_by_tariff(rows: list[dict]):
    """Yield (tariff, [(row, points), ...]) sorted by total desc. The module-coverage
    denominator is the best coverage any model achieved in this batch (so 'full' is
    self-calibrating and the score stays schema-shape-agnostic)."""
    max_modules = max((r["modules_max"] for r in rows), default=1) or 1
    by_tariff: dict[str, list[dict]] = {}
    for r in rows:
        by_tariff.setdefault(r["tariff"], []).append(r)
    for tariff in sorted(by_tariff):
        scored = sorted(((r, score_row(r, max_modules)) for r in by_tariff[tariff]),
                        key=lambda rs: rs[1]["total"], reverse=True)
        yield tariff, scored


def load_results(path: Path | None = None) -> dict:
    """Load the durable benchmark digest -> {generated, commit, models, repeat, rows}.
    Returns {} if the file is absent or unreadable so callers can show an empty state
    instead of crashing (the TUI Benchmark tab relies on this)."""
    p = path or RESULTS_JSON
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _selftest() -> int:
    rows = [
        {"tariff": "t/a", "model": "perfect", "input": "x", "runs": 3, "ok": 3,
         "schema_ok": 3, "faithful": 3, "modules_min": 8, "modules_max": 8,
         "unsupported_max": 0, "cost_usd": None, "wall_s": 100},
        {"tariff": "t/a", "model": "halluc", "input": "x", "runs": 3, "ok": 3,
         "schema_ok": 3, "faithful": 1, "modules_min": 8, "modules_max": 8,
         "unsupported_max": 2, "cost_usd": 1.0, "wall_s": 90},
        {"tariff": "t/a", "model": "dnf", "input": "x", "runs": 3, "ok": 0,
         "schema_ok": 0, "faithful": 0, "modules_min": 0, "modules_max": 0,
         "unsupported_max": 0, "cost_usd": None, "wall_s": None},
    ]
    by = {t: s for t, s in scored_by_tariff(rows)}
    scored = by["t/a"]
    # Ranking: perfect first, dnf last.
    assert [r["model"] for r, _ in scored] == ["perfect", "halluc", "dnf"], scored
    perfect = next(s for r, s in scored if r["model"] == "perfect")
    assert round(perfect["total"]) == 100, perfect
    # halluc: faithful 50/3=16.7 + schema 20 + halluc max(0,15-10)=5 + modules 15 = 56.7
    halluc = next(s for r, s in scored if r["model"] == "halluc")
    assert round(halluc["total"]) == 57, halluc
    dnf = next(s for r, s in scored if r["model"] == "dnf")
    assert dnf["dnf"] and dnf["total"] == 0.0, dnf
    # Module credit is gated on schema validity: breadth over invalid output = 0.
    invalid = score_row({"runs": 3, "ok": 3, "schema_ok": 0, "faithful": 0,
                         "modules_max": 8, "unsupported_max": 0}, 8)
    assert invalid["modules"] == 0.0 and invalid["schema"] == 0.0, invalid
    print("scorecard selftest OK")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
