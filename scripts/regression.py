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
import unicodedata
from pathlib import Path

from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _vertical  # noqa: E402
from _manifest import load_manifest  # noqa: E402

ROOT = _vertical.ROOT
GOLDEN = _vertical.golden_path()
SCHEMA = _vertical.tariff_schema_path()
TARIFFS = _vertical.tariffs_dir()

# Tokens that carry no product identity — they appear in nearly every document
# filename and would make the attribution check below pass on noise alone. The
# universal core (legal forms, document nouns, German stopwords) lives here;
# vertical-specific noise tokens ("rechtsschutz", "privat", ...) are DATA in
# config/verticals/<v>/vertical.json under `regression_generic_tokens`.
_CORE_GENERIC_TOKENS = {
    "versicherung", "versicherungsbedingungen", "allgemeine",
    "produktinformationsblatt", "weitere", "unterlagen", "ag", "se", "gmbh",
    "und", "mit", "der", "die", "das", "fuer", "von", "als", "pdf", "kombiniert",
    "besondere",
}
_GENERIC_TOKENS = _CORE_GENERIC_TOKENS | {
    str(t) for t in _vertical.vertical_config().get("regression_generic_tokens", [])
}
# A manifest entry passes when at least this share of its tariff-name tokens shows up
# in its own document filenames. Calibrated over the 26 tracked entries: the known
# mis-attribution scores 0.25, the weakest legitimate entry 0.80, all others 1.00.
_ATTRIBUTION_MIN = 0.5


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
        try:
            return val in exp, f"{val!r} in {exp}"
        except TypeError:
            return False, (f"value of type {type(val).__name__} cannot be tested "
                           f"for membership in {exp!r}")
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


def check_market_record(record: dict, schema: dict) -> list[str]:
    """Market-wide checks applied to EVERY out/tariffs/*.json, not just the two
    golden-pinned stems: schema conformance plus the beitrag-null pipeline
    invariant (out/tariffs/ must never carry a premium -- that only ever belongs
    in out/enriched/ via overlay.py; see the golden 'isnull' invariants for the
    same semantics). Deliberately narrow: qualitative selbstbeteiligung data is
    legitimate and not checked here."""
    violations = [f"schema: {err}" for err in schema_errors(record, schema)]
    for path in ("beitrag.monatlich_eur", "beitrag.jaehrlich_eur"):
        val = get_path(record, path)
        if val is not None:
            violations.append(
                f"{path} [isnull]: {val!r} is not null "
                f"(out/tariffs must never carry a premium -- use overlay.py/out/enriched instead)"
            )
    return violations


def _identity_tokens(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return [t for t in re.split(r"[^a-z0-9]+", folded)
            if len(t) >= 2 and t not in _GENERIC_TOKENS]


def check_attribution(entry: dict) -> str:
    """Does a manifest entry's own document set belong to the tariff it names?

    harvest_docs resolves a stem partly from the filestore bundle hash, but that hash
    identifies a document BUNDLE, not a tariff, and CHECK24 serves one bundle for many
    tariffs. A shared bundle could therefore land on a foreign tariff's entry — which is
    how arag__komfort-2026 came to hold Provinzial Rheinland documents, and
    arag__premium-flex-familienrecht-2026 the ARAG Komfort set, both undetected for five
    weeks. Nothing downstream notices: the PDFs are real, the schema fits, and the
    extracted record simply describes the wrong product.

    Compares the tariff name against the document filenames, which CHECK24 derives from
    the product itself. Returns "" on pass, else a violation string.
    """
    wanted = _identity_tokens(entry.get("tariff", ""))
    if not wanted:
        return ""
    have = set(_identity_tokens(" ".join(d.get("file", "") for d in entry.get("docs", []))))
    if not have:
        # Every filename is fully generic ("Produktinformationsblatt",
        # "Allgemeine_Versicherungsbedingungen" — how the PHV/Hausrat filestore
        # names its documents): there is no product identity in the names at all,
        # so no filename heuristic can attribute or mis-attribute the bundle.
        # Un-checkable is a pass, mirroring the no-token tariff-name case above.
        return ""
    hits = [t for t in wanted if t in have]
    share = len(hits) / len(wanted)
    if share >= _ATTRIBUTION_MIN:
        return ""
    sample = next((d.get("file", "") for d in entry.get("docs", [])), "(no documents)")
    return (f"attribution: only {len(hits)}/{len(wanted)} tariff-name token(s) of "
            f"{entry.get('tariff')!r} appear in its document filenames "
            f"(e.g. {sample!r}) — the documents likely belong to another tariff; "
            f"re-harvest this stem")


def current_document_hashes() -> dict[str, dict[str, str]] | None:
    """stem -> {doctype: content_sha256} for the texts extract.py would read today.

    Returns None when data/extracted/manifest.json is absent — it is gitignored (the
    texts are derived from third-party PDFs), so CI and fresh clones legitimately lack
    it and the staleness check below simply does not run there.
    """
    path = _vertical.extracted_dir() / "manifest.json"
    try:
        docs = json.loads(path.read_text(encoding="utf-8"))["documents"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return None
    out: dict[str, dict[str, str]] = {}
    for d in docs:
        stem = f"{d.get('insurer', '')}__{d.get('tariff', '')}"
        if d.get("doctype") and d.get("content_sha256"):
            out.setdefault(stem, {})[d["doctype"]] = d["content_sha256"]
    return out


def check_staleness(stem: str, record: dict, current: dict[str, dict[str, str]]) -> str:
    """Was this record extracted from the documents that are on disk now?

    The attribution gate above validates the MANIFEST; this validates the RECORD against
    it. The two diverge exactly when a mis-attributed entry gets re-harvested: the
    manifest and the PDFs are then correct while out/tariffs/ still holds facts read
    from the previous, wrong documents — a state the attribution gate reports as fully
    healthy. extract.py resolves it on its next successful run (the input hash no longer
    matches, so it is a cache miss), but until then the record is silently wrong.

    Returns "" on pass, else a violation string.
    """
    have = current.get(stem)
    if not have:  # tariff not in the extracted set (e.g. never ingested) — not our call
        return ""
    was = {s.get("doctype"): s.get("content_sha256")
           for s in record.get("sources", []) if isinstance(s, dict)}
    if not was:
        return ""
    if was == have:
        return ""
    added = sorted(set(have) - set(was))
    dropped = sorted(set(was) - set(have))
    changed = sorted(d for d in set(was) & set(have) if was[d] != have[d])
    detail = ", ".join(filter(None, [
        f"changed: {', '.join(changed)}" if changed else "",
        f"new documents: {', '.join(added)}" if added else "",
        f"no longer present: {', '.join(dropped)}" if dropped else "",
    ]))
    return (f"stale: the record was extracted from different documents than the ones on "
            f"disk ({detail}) — re-run extract.py for this stem")


def main() -> int:
    ap = argparse.ArgumentParser(description="Check tariff records against golden invariants.")
    ap.add_argument("--golden", default=str(GOLDEN), help="golden invariants file")
    ap.add_argument("--record", default=None,
                    help="check a single record file against the matching tariff "
                         "(by filename stem) instead of all of out/tariffs/")
    ap.add_argument("--since", metavar="DATE",
                    help="only check stems whose out/tariffs/ file was modified on or "
                         "after DATE (YYYY-MM-DD) — useful as a post-extract CI filter")
    ap.add_argument("--all-verticals", action="store_true",
                    help="run the checks once per non-disabled registry vertical "
                         "(each in a subprocess with CHECK0R_VERTICAL set); the "
                         "worst return code wins")
    args = ap.parse_args()

    if args.all_verticals:
        import os
        import subprocess
        rc = 0
        for v in _vertical.selectable():
            print(f"\n===== vertical: {v} =====", flush=True)
            res = subprocess.run(
                [sys.executable, __file__],
                env={**os.environ, "CHECK0R_VERTICAL": v})
            rc = max(rc, res.returncode)
        return rc

    # A vertical without a curated golden.json (freshly scaffolded) still gets the
    # market-wide sweep — golden-less means "no pinned invariants yet", not "skip".
    golden_file = Path(args.golden)
    if golden_file.exists():
        golden_doc = json.loads(golden_file.read_text(encoding="utf-8"))
    else:
        print(f"(no golden invariants at {golden_file} — market sweep only)")
        golden_doc = {"tariffs": {}}
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

    # Market-wide pass: schema + the beitrag-null invariant over EVERY out/tariffs/
    # record, not just the two golden-pinned stems. golden.json only ever curates a
    # handful of stems (hand-verified against the source PDFs), so without this pass
    # most of the market silently got zero regression coverage. --record targets a
    # single file for golden comparison, so skip the market sweep in that mode.
    market_targets: list[Path] = []
    market_failed = 0
    if not args.record:
        market_targets = sorted(TARIFFS.glob("*.json"))
        if market_targets:
            print()
            print(f"--- market-wide ({len(market_targets)} out/tariffs/*.json): "
                  f"schema + beitrag-null ---")
            current_docs = current_document_hashes()
            if current_docs is None:
                print("  (staleness check skipped: data/extracted/manifest.json absent)")
            for path in market_targets:
                record = json.loads(path.read_text(encoding="utf-8"))
                violations = check_market_record(record, schema)
                if current_docs is not None:
                    stale = check_staleness(path.stem, record, current_docs)
                    if stale:
                        violations.append(stale)
                if violations:
                    market_failed += 1
                    print(f"FAIL  {path.stem}  ({len(violations)} violation(s))")
                    for v in violations:
                        print(f"        - {v}")
                else:
                    print(f"PASS  {path.stem}")

    # Manifest attribution: catches a stem whose DOCUMENTS belong to another tariff.
    # Independent of the record checks above — a mis-attributed entry produces a
    # perfectly schema-valid record, it just describes the wrong product.
    attribution_failed = 0
    n_entries = 0
    if not args.record and not _vertical.manifest_path().exists():
        # A freshly scaffolded vertical has no doc manifest yet; load_manifest()
        # would sys.exit. Nothing to attribute — skip with a note.
        print()
        print("(manifest attribution skipped: no doc manifest for this vertical yet)")
    elif not args.record:
        entries = load_manifest()["tariffs"]
        n_entries = len(entries)
        print()
        print(f"--- manifest attribution ({n_entries} entries): "
              f"documents vs. tariff name ---")
        for entry in sorted(entries, key=lambda e: e.get("stem", "")):
            violation = check_attribution(entry)
            if violation:
                attribution_failed += 1
                print(f"FAIL  {entry.get('stem')}\n        - {violation}")
        if not attribution_failed:
            print(f"PASS  all {n_entries} entries carry their own tariff's documents")

    print()
    if failed or market_failed or attribution_failed:
        extra = (f", {market_failed}/{len(market_targets)} market-wide tariff(s) failed"
                 if market_targets else "")
        if attribution_failed:
            extra += f", {attribution_failed}/{n_entries} manifest entry/entries mis-attributed"
        print(f"REGRESSION: {failed}/{len(targets)} golden tariff(s) failed{extra}. "
              f"The extraction no longer produces the document-grounded facts.",
              file=sys.stderr)
        return 1
    extra = f"; {len(market_targets)} market-wide tariff(s) pass schema + beitrag-null" if market_targets else ""
    if n_entries:
        extra += f"; {n_entries} manifest entry/entries correctly attributed"
    print(f"OK: {len(targets)} tariff(s) pass all invariants{extra}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
