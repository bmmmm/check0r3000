#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Enforce the golden `isnull` pins on out/tariffs/ records (auto-repair).

benchmarks/golden.json pins fields the source documents structurally cannot
support: modules.*.level (the AVB lists Basis/Komfort/Premium side by side and
never states a chosen Stufe) and beitrag.* (never in AVB/PIB). A re-extract with
a cheap model can hallucinate them back — typically level='Premium' from the
product NAME ("TIER-AS-PRODUCT" scenario). Unlike eq/contains/nonnull invariants,
which describe facts that could legitimately change when the documents change, a
violated isnull pin is always extraction noise — so re-nulling it is safe and
closes the gap outright instead of relying on someone noticing a warn-only
regression FAIL. regression.py stays the detector for everything else.

Runs after extract in every pipeline funnel (pipeline.sh + TUI tail).

Reads:   benchmarks/golden.json + out/tariffs/<stem>.json (golden stems only)
Writes:  repaired out/tariffs/<stem>.json (+ a tariff-history version) and
         tmp/golden-pin-repairs.json (always written, [] when clean)
Exit:    always 0 — this IS the fix, not another gate.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import feature_history  # noqa: E402
import _vertical  # noqa: E402
from _jsonio import atomic_write_json, load_json_or  # noqa: E402

GOLDEN = _vertical.golden_path()
TARIFFS = _vertical.tariffs_dir()
SIDECAR = _vertical.TMP / "golden-pin-repairs.json"


def _get_path(obj, path: str):
    """Walk a dotted path; a missing or non-dict segment yields None.

    Duplicated from regression.get_path deliberately: importing regression would
    pull its unconditional jsonschema dependency into a script that never
    validates a schema.
    """
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _set_null(obj: dict, path: str) -> bool:
    """Null the field at a dotted path; False when the parent path is absent."""
    parts = path.split(".")
    cur = obj
    for part in parts[:-1]:
        cur = cur.get(part) if isinstance(cur, dict) else None
        if cur is None:
            return False
    if not isinstance(cur, dict):
        return False
    cur[parts[-1]] = None
    return True


def enforce_stem(stem: str, record: dict, invariants: list[dict]) -> list[dict]:
    """Re-null every violated `isnull` invariant; returns the repairs made."""
    repairs: list[dict] = []
    for inv in invariants:
        if inv.get("check") != "isnull":
            continue
        old = _get_path(record, inv["path"])
        if old is not None and _set_null(record, inv["path"]):
            repairs.append({"stem": stem, "path": inv["path"], "old_value": old,
                            "why": inv.get("why", "")})
    return repairs


def main() -> int:
    golden_doc = load_json_or(GOLDEN, None)
    if not isinstance(golden_doc, dict):
        print("golden_pins: benchmarks/golden.json missing/unreadable — nothing to do",
              file=sys.stderr)
        return 0
    all_repairs: list[dict] = []
    for stem, entry in sorted(golden_doc.get("tariffs", {}).items()):
        path = TARIFFS / f"{stem}.json"
        record = load_json_or(path, None)
        if not isinstance(record, dict):
            continue
        repairs = enforce_stem(stem, record, entry.get("invariants", []))
        if repairs:
            # The repaired record replaces the hallucinated one under its own
            # _input_hash: the cache keeps it, and the same-day history archive is
            # overwritten with the corrected state.
            atomic_write_json(path, record)
            feature_history.archive_version(stem, record)
            all_repairs.extend(repairs)
            for r in repairs:
                print(f"REPAIRED {stem}: {r['path']} was {r['old_value']!r} -> null"
                      f"  ({r['why']})", file=sys.stderr)
    SIDECAR.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(SIDECAR, all_repairs)
    print("golden_pins: OK — no pinned field violated" if not all_repairs
          else f"golden_pins: {len(all_repairs)} field(s) auto-repaired "
               f"(see tmp/golden-pin-repairs.json)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
