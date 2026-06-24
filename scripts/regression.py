#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["jsonschema>=4"]
# ///
"""Regression guard: assert extracted tariff records still satisfy known,
document-grounded invariants.

This is the answer to "do we notice when the extraction prompt/model stops
working?". benchmarks/golden.json pins, per tariff, the facts the source
documents actually state -- and the ones they deliberately do NOT (premium,
chosen Stufe), which must stay null. Each invariant cites why it holds.

Any violation exits non-zero, so a prompt or model change that degrades the
output (dropped fields, hallucinated Stufe, null identity, invented premium)
is caught instead of silently shipping a worse out/tariffs/*.json. Keyed by the
out/tariffs/<key>.json filename stem, which is derived from the manifest and is
stable across models.

Run:  python3 scripts/regression.py                  # check out/tariffs/*.json
      python3 scripts/regression.py --record FILE     # check a single record
      uv run scripts/regression.py
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "benchmarks" / "golden.json"
SCHEMA = ROOT / "schema" / "tariff.schema.json"
TARIFFS = ROOT / "out" / "tariffs"


def norm_digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def get_path(obj, path: str):
    """Walk a dotted path; a missing or non-dict segment yields None."""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def check_invariant(record: dict, inv: dict) -> tuple[bool, str]:
    """Evaluate one invariant. Returns (ok, human-readable detail)."""
    val = get_path(record, inv["path"])
    chk = inv["check"]
    exp = inv.get("expected")
    if chk == "eq":
        return val == exp, f"{val!r} == {exp!r}"
    if chk == "nonnull":
        return val not in (None, "", []), f"{val!r} is set"
    if chk == "isnull":
        return val is None, f"{val!r} is null"
    if chk == "contains":
        ok = val is not None and str(exp).lower() in str(val).lower()
        return ok, f"{exp!r} in {val!r}"
    if chk == "contains_all_digits":
        digits = norm_digits(str(val)) if val is not None else ""
        missing = [n for n in exp if n not in digits]
        return not missing, (f"digits {exp} present" if not missing
                             else f"missing {missing} in {val!r}")
    if chk == "in":
        return val in exp, f"{val!r} in {exp}"
    raise ValueError(f"unknown check kind: {chk!r} (path {inv['path']})")


def schema_errors(record: dict, schema: dict) -> list[str]:
    """Validate against the schema with `sources` optional.

    Records in out/tariffs/ carry pipeline-injected provenance, but a record
    handed in fresh from a model may not; provenance is never the model's job,
    so a missing `sources` is not a regression.
    """
    model_schema = dict(schema)
    model_schema["required"] = [r for r in schema.get("required", []) if r != "sources"]
    # Drop provenance (`sources`) and pipeline bookkeeping (`_input_hash`, `_model`,
    # `_filter`): none of it is the model's factual output, and the public schema
    # stays clean of caching metadata.
    rec = {k: v for k, v in record.items() if k != "sources" and not k.startswith("_")}
    errs = sorted(Draft202012Validator(model_schema).iter_errors(rec),
                  key=lambda e: list(e.path))
    return [f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errs[:5]]


def check_record(record: dict, golden: dict, schema: dict) -> list[str]:
    """Return a list of violation strings (empty == pass)."""
    violations = []
    for err in schema_errors(record, schema):
        violations.append(f"schema: {err}")
    for inv in golden["invariants"]:
        ok, detail = check_invariant(record, inv)
        if not ok:
            violations.append(f"{inv['path']} [{inv['check']}]: {detail}  ({inv['why']})")
    return violations


def main() -> int:
    ap = argparse.ArgumentParser(description="Check tariff records against golden invariants.")
    ap.add_argument("--golden", default=str(GOLDEN), help="golden invariants file")
    ap.add_argument("--record", default=None,
                    help="check a single record file against the matching tariff "
                         "(by filename stem) instead of all of out/tariffs/")
    args = ap.parse_args()

    golden_doc = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    tariffs = golden_doc["tariffs"]

    if args.record:
        path = Path(args.record)
        key = path.stem
        if key not in tariffs:
            print(f"error: no golden invariants for '{key}' "
                  f"(known: {', '.join(sorted(tariffs))})", file=sys.stderr)
            return 2
        targets = [(key, path)]
    else:
        targets = [(key, TARIFFS / f"{key}.json") for key in sorted(tariffs)]

    failed = 0
    for key, path in targets:
        if not path.exists():
            print(f"FAIL  {key}: no record at {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")
            failed += 1
            continue
        record = json.loads(path.read_text(encoding="utf-8"))
        violations = check_record(record, tariffs[key], schema)
        n = len(tariffs[key]["invariants"])
        if violations:
            failed += 1
            print(f"FAIL  {key}  ({len(violations)} violation(s) / {n} invariants + schema)")
            for v in violations:
                print(f"        - {v}")
        else:
            print(f"PASS  {key}  ({n} invariants + schema)")

    print()
    if failed:
        print(f"REGRESSION: {failed}/{len(targets)} tariff(s) failed. "
              f"The extraction no longer produces the document-grounded facts.",
              file=sys.stderr)
        return 1
    print(f"OK: {len(targets)} tariff(s) pass all invariants.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
