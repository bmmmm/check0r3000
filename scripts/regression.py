#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "jsonschema>=4",
# ]
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


def amounts(s: str) -> set[str]:
    """The German-formatted numbers in `s` as plain digit strings: thousands grouping
    ('.', spaces, NBSP, narrow-NBSP) is dropped and a ',<digits>' decimal fraction is
    discarded, but distinct numbers stay distinct. '1.503,00 EUR' -> {'1503'};
    '150 / 300' -> {'150', '300'}. Compared as whole numbers, never as substrings of a
    concatenated digit stream (which let '1.503,00' satisfy ['150', '300'])."""
    s = s.replace("\u00a0", " ").replace("\u202f", " ")
    s = re.sub(r",\d+", "", s)
    return {re.sub(r"[.\s]", "", m) for m in re.findall(r"\d{1,3}(?:[.\s]\d{3})+|\d+", s)}


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
        nums = amounts(str(val)) if val is not None else set()
        missing = [n for n in exp if n not in nums]
        return not missing, (f"digits {exp} present" if not missing
                             else f"missing {missing} in {val!r}")
    if chk == "in":
        if not isinstance(exp, (list, tuple, set, str)):
            raise ValueError(f"'in' check needs a container in 'expected' "
                             f"(path {inv['path']}, got {type(exp).__name__})")
        return val in exp, f"{val!r} in {exp}"
    raise ValueError(f"unknown check kind: {chk!r} (path {inv['path']})")


def schema_errors(record: dict, schema: dict) -> list[str]:
    """Validate against the schema with `sources` optional.

    Records in out/tariffs/ carry pipeline-injected provenance, but a record
    handed in fresh from a model may not; provenance is never the model's job,
    so a missing `sources` is not a regression.
    """
    if not isinstance(record, dict):
        # A model may emit a top-level list/scalar/null; reporting it as one clean
        # violation keeps the whole run from aborting on `.items()` (the guard would
        # otherwise go blind exactly when the extraction degraded the most).
        return [f"<root>: record is not a JSON object (got {type(record).__name__})"]
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
    ap.add_argument("--since", metavar="DATE",
                    help="only check stems whose out/tariffs/ file was modified on or "
                         "after DATE (YYYY-MM-DD) — useful as a post-extract CI filter")
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

    if args.since:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", args.since):
            print(f"error: --since expects YYYY-MM-DD, got {args.since!r}", file=sys.stderr)
            return 2
        import datetime
        since_dt = datetime.date.fromisoformat(args.since)

        def _file_is_recent(p: Path) -> bool:
            if not p.exists():
                return True  # include so it fails with a proper error
            return datetime.date.fromtimestamp(p.stat().st_mtime) >= since_dt

        before = len(targets)
        targets = [(key, path) for key, path in targets if _file_is_recent(path)]
        skipped = before - len(targets)
        if skipped:
            print(f"[--since {args.since}] skipped {skipped} stem(s) not re-extracted since then.")
        if not targets:
            print(f"No tariffs re-extracted since {args.since} — nothing to check.")
            return 0

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
