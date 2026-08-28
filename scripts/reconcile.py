#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Reconcile re-extracted out/tariffs/*.json against HEAD so a re-extract can never
*regress* a record. The --repeat union only merges leistungen/ausschluesse across the
fresh runs; modules/coverage come from the single "most complete" fresh run, so a
prompt-too-long run can drop modules below what HEAD already had. This merge takes, per
field, the better of (HEAD, working-tree):

  modules  : the block with MORE included Bausteine (tie -> fresh)
  coverage : the block with MORE non-empty fields      (tie -> fresh)
  leistungen / ausschluesse / besonderheiten : union (order-preserving, fresh first)
  scalars (insurer/tariff/stand) : fresh if non-null else HEAD
  beitrag  : forced null (pipeline invariant)
  provenance (_model/_filter/_repeat/...) : fresh; + _reconciled marker

The two golden+curated stems (advocard 360, arag premium 2026) are skipped entirely —
they were never re-extracted and carry hand patches we must not touch.

Usage: reconcile.py [--apply]   (default: dry-run)
"""
import json
import subprocess
import sys
from pathlib import Path

# repo root is the check0r3000 working dir; resolve via git
REPO_ROOT = Path(
    subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip()
)
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import coverage_taxonomy  # noqa: E402
import feature_history  # noqa: E402
import _vertical  # noqa: E402
from _jsonio import atomic_write_json  # noqa: E402

OUT = _vertical.tariffs_dir()

# Primary guard is the "_curated": true marker in the record itself (extract.py refuses
# to re-extract a curated stem without --force); this hardcoded set is a second,
# reconcile-local safety net so those records are never touched here either. roland is
# golden-PINNED, not curated: its modules.*.level nulls are enforced by golden_pins.py,
# and this merge's blunt whole-block comparison would happily keep a hallucinated
# non-null level from the "more complete" side — so it must not touch roland either.
SKIP = {"advocard__360-privat-rechtsschutz", "arag__premium-2026",
        "roland__top-rechtsschutz-premium"}

LIST_FIELDS = ("leistungen", "ausschluesse", "besonderheiten")


def head_version(rel: str):
    try:
        raw = subprocess.check_output(["git", "show", f"HEAD:{rel}"],
                                      stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        return None
    return json.loads(raw)


def n_included(rec):
    return sum(1 for m in (rec.get("modules") or {}).values()
               if isinstance(m, dict) and m.get("included"))


def n_cov(rec):
    cov = rec.get("coverage") or {}
    return sum(1 for v in cov.values() if v not in (None, "", [], {}))


def union(a, b):
    """Order-preserving union: a's items first, then b-only items.

    Dedups on coverage_taxonomy.normalize(), mirroring extract.merge_records()'s
    semantics — otherwise a case/glyph variant of the same benefit (e.g.
    "Mediation" vs "MEDIATION") that merge_records would have collapsed into one
    entry gets double-counted here. Non-string items are dropped, same as
    merge_records (leistungen/ausschluesse/besonderheiten are schema-typed as string
    arrays; a non-string item is an extraction bug, not a distinct value)."""
    seen: dict[str, str] = {}
    for it in list(a or []) + list(b or []):
        if not isinstance(it, str):
            continue
        key = coverage_taxonomy.normalize(it)
        if key and key not in seen:
            seen[key] = it
    return list(seen.values())


def merge(head, fresh):
    """Merge HEAD into fresh, never regressing. Returns (record, changed_fields)."""
    out = dict(fresh)  # fresh provenance + scalars by default
    changed = []

    # modules: keep the block with more included Bausteine
    if n_included(head) > n_included(fresh):
        out["modules"] = head.get("modules")
        changed.append(f"modules {n_included(fresh)}->{n_included(head)}")

    # coverage: keep the block with more populated fields
    if n_cov(head) > n_cov(fresh):
        out["coverage"] = head.get("coverage")
        changed.append(f"coverage {n_cov(fresh)}->{n_cov(head)}")

    # list fields: union (fresh first)
    for fld in LIST_FIELDS:
        merged = union(fresh.get(fld), head.get(fld))
        if len(merged) != len(fresh.get(fld) or []):
            changed.append(f"{fld} {len(fresh.get(fld) or [])}->{len(merged)}")
        out[fld] = merged

    # scalars: fresh if non-null else HEAD
    for fld in ("insurer", "tariff", "stand"):
        if not fresh.get(fld) and head.get(fld):
            out[fld] = head[fld]
            changed.append(f"{fld}<-HEAD")

    out["beitrag"] = None  # invariant
    if changed:
        out["_reconciled"] = True
    return out, changed


def main():
    apply = "--apply" in sys.argv
    total_changed = 0
    for f in sorted(OUT.glob("*.json")):
        stem = f.stem
        if stem in SKIP:
            continue
        rel = (OUT / f"{stem}.json").relative_to(REPO_ROOT).as_posix()
        head = head_version(rel)
        if head is None:
            continue
        fresh = json.loads(f.read_text(encoding="utf-8"))
        # only reconcile stems that were actually re-extracted (differ from HEAD)
        if fresh == head:
            continue
        merged, changed = merge(head, fresh)
        if not changed:
            continue
        total_changed += 1
        print(f"{stem}: {', '.join(changed)}")
        if apply:
            atomic_write_json(f, merged)
            if feature_history.archive_version(stem, merged):
                print(f"    history archived")
    print(f"\n{'APPLIED' if apply else 'DRY-RUN'}: {total_changed} record(s) "
          f"{'merged' if apply else 'would be merged'}")


if __name__ == "__main__":
    main()
