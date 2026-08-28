#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Extract stage: turn each tariff's documents into structured comparison facts.

For every (insurer, tariff) in data/extracted/manifest.json, concatenate the
extracted document texts and ask `claude -p` to emit a JSON record conforming to
schema/tariff.schema.json. Results are cached by an input hash so re-runs are free
unless a document or the prompt changed.

Reads:   data/extracted/manifest.json + data/extracted/**/*.txt
Writes:  out/tariffs/<insurer>__<tariff>.json

Models are addressed by spec (see scripts/_providers.py): a bare name or claude:X
is the `claude` CLI; ollama:/mlx:/openai: route to a local OpenAI-compatible server.
Pure stdlib — no uv deps needed:
Run:  python3 scripts/extract.py                  (all tariffs, cached)
      python3 scripts/extract.py --force           (ignore cache)
      python3 scripts/extract.py --model opus
      python3 scripts/extract.py --model haiku --filter   (trim oversized AVBs)
      python3 scripts/extract.py --model ollama:llama3.1:8b
      python3 scripts/extract.py --only arag__premium-2026 --repeat 3
      python3 scripts/extract.py --model haiku --jobs 4   (parallel; mind rate limits,
                                                             2-4 is a reasonable range)
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import hashlib
import json
import os
import re
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _filter  # noqa: E402
import _jsonio  # noqa: E402
import _providers  # noqa: E402
import coverage_taxonomy  # noqa: E402
import feature_history  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
EXTRACTED = ROOT / "data" / "extracted"
SCHEMA = ROOT / "schema" / "tariff.schema.json"
OUT = ROOT / "out" / "tariffs"

# Bump when the prompt/schema semantics change to invalidate all caches.
PROMPT_VERSION = "4"

INSTRUCTION = """You are extracting structured, comparable facts from a German \
legal-protection-insurance (Rechtsschutzversicherung) document set.

The stdin input contains:
  1. A JSON Schema describing the exact output shape.
  2. One or more documents (AVB, Produktinformationsblatt, weitere Unterlagen, \
Leistungsübersicht), each prefixed with a `===== <doctype> =====` header.

Rules:
- Output ONLY one JSON object that validates against the schema. No prose, no \
code fences.
- Facts only. Do NOT copy verbatim policy text into the output.
- Use null / empty arrays where the documents do not state a value. NEVER guess \
a number.
- For module `level`, use Basis/Komfort/Premium ONLY if the documents state which \
variant THIS tariff actually has. If they merely list the variants as selectable \
options without naming the chosen one (typical for AVB/PIB), use null — never guess \
a level.
- For `coverage.selbstbeteiligung`: if the documents establish that a deductible \
applies (a 'vereinbarte Selbstbeteiligung', a per-scenario amount, or a waiver \
clause) but state no single fixed tariff amount, describe the arrangement \
qualitatively (e.g. 'vereinbart, Höhe im Versicherungsschein') instead of null. \
Describing a stated arrangement is not guessing a number.
- Keep array entries short (a few words each), in German.
- Omit the `sources` field entirely — the pipeline adds provenance; never invent \
content hashes."""


def slug(s: str) -> str:
    s = s.lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n", "", t)
        t = re.sub(r"\n```\s*$", "", t)
    return t.strip()


def _balanced_spans(t: str) -> list[str]:
    """Every balanced top-level {...} span, respecting strings/escapes.

    A greedy brace regex over-matches: a stray brace in prose (before OR after the
    JSON), or a second object, makes one captured span unparseable. Tracking brace
    depth — and skipping braces inside string literals — yields each complete
    candidate object separately so the caller can pick the one that actually parses.
    """
    spans: list[str] = []
    depth, start, in_str, esc = 0, None, False, False
    for i, ch in enumerate(t):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                spans.append(t[start:i + 1])
                start = None
    return spans


def coerce_json(text: str) -> dict:
    """Parse the model's reply into a dict, tolerating fences or surrounding prose.

    Large inputs make models more likely to wrap the JSON in explanatory text. If the
    whole reply will not parse, scan for balanced brace spans and return the largest
    one that parses to an object — robust against prose braces and a stray second
    object, where a greedy outermost-span match silently produced garbage.
    """
    t = strip_fences(text)
    try:
        obj = json.loads(t)
        # Only a top-level object satisfies the contract. A valid top-level array or
        # scalar must NOT be returned as-is: callers index it like a dict
        # (record["_input_hash"], record.get(...)) and would crash. Fall through to the
        # brace scan, which recovers the largest embedded object (e.g. "[ {…} ]").
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    candidates: list[dict] = []
    for span in _balanced_spans(t):
        try:
            obj = json.loads(span)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            candidates.append(obj)
    if candidates:
        return max(candidates, key=lambda d: len(json.dumps(d)))
    raise json.JSONDecodeError("no parseable JSON object in model reply", t, 0)


def build_payload(schema_text: str, docs: list[dict], root: Path,
                  transform=None) -> str:
    """Concatenate the schema and each document into one stdin payload.

    `transform(doctype, text) -> text` optionally rewrites a document before it is
    appended (used to trim oversized AVBs). Shared with scripts/eval.py so the
    benchmark scores the exact same input the pipeline uses.
    """
    parts = [f"===== SCHEMA =====\n{schema_text}\n"]
    for d in docs:
        text = (root / d["extracted_path"]).read_text(encoding="utf-8")
        if transform:
            text = transform(d["doctype"], text)
        parts.append(f"===== {d['doctype']} =====\n{text}\n")
    return "\n".join(parts)


# Per-AVB character budget for --filter, calibrated against real calls rather than set
# to a round number. Trimming costs recall, so the budget belongs as HIGH as the model
# still accepts:
#   AVB 321k -> payload 391k  rejected (0 tokens, 0 cost, bare stop_sequence)
#   AVB 283k -> payload 350k  accepted, 54 benefits extracted
#   AVB 239k -> payload 310k  accepted, but only 26 benefits — half the recall
# The first budget tried here was 250k, and that 26-vs-54 gap is what it cost: a
# document that fits is worthless if the clauses got cut out of it. 290k keeps the
# largest window that still goes through, and binds on the ARAG family alone — every
# other AVB trims below it and keeps both its clause context and its cached extraction.
AVB_MAX_CHARS = 290_000


def avb_transform(doctype: str, text: str) -> str:
    """Trim only the AVB (the oversized document); pass others through unchanged."""
    return (_filter.filter_text(text, max_chars=AVB_MAX_CHARS)
            if doctype == "avb" else text)


# Fields whose cross-run UNION we keep when --repeat > 1. Run-to-run variance in
# cheap models shows up as *omission*: a real benefit/exclusion present in one run is
# dropped in another (observed between near-identical sibling tariffs). Unioning these
# two string-array fields maximizes recall — which is exactly what stabilizes the
# downstream leistung_cov score. Every other field is taken verbatim from the single
# most-complete run, because a cross-run merge of structured/scalar data (module
# levels, coverage amounts, notes) would risk inventing a combination no run produced.
_UNION_FIELDS = ("leistungen", "ausschluesse")

# Run-level stats for the update report. Cost is run metadata, not record content —
# records stay content-hashed, so the totals land in a tmp/ sidecar (written by main()),
# never in a record field. Workers under --jobs share these, hence the lock.
_STATS_LOCK = threading.Lock()
_RUN_STATS = {"cost_usd": 0.0, "priced_calls": 0, "unpriced_calls": 0,
              "cached": 0, "shared": 0, "extracted": 0, "failed": 0}


def _record_cost(cost_usd) -> None:
    with _STATS_LOCK:
        if isinstance(cost_usd, (int, float)):
            _RUN_STATS["cost_usd"] += cost_usd
            _RUN_STATS["priced_calls"] += 1
        else:
            # Local backends (omlx:/mlx:/ollama:) report cost_usd = null.
            _RUN_STATS["unpriced_calls"] += 1


def _tally(kind: str) -> None:
    with _STATS_LOCK:
        _RUN_STATS[kind] += 1


def extract_once(model: str, payload: str) -> tuple[dict | None, str | None, float | None]:
    """One model call → (record, error, cost_usd). No side effects."""
    result = _providers.run(model, INSTRUCTION, payload)
    cost = result.get("cost_usd")
    if result["error"] or not result["text"]:
        return None, result["error"] or "empty response", cost
    try:
        return coerce_json(result["text"]), None, cost
    except Exception as e:  # noqa: BLE001 — surface any parse failure as a run error
        return None, f"could not parse JSON: {e}", cost


def merge_records(records: list[dict]) -> dict:
    """Stabilize N extraction runs of the SAME tariff into one record.

    Base = the single most-complete run (longest serialization), copied verbatim so
    structured fields stay internally consistent. Then `_UNION_FIELDS` are replaced by
    the deduplicated union across all runs (base's items first, novel items from other
    runs appended), deduped by `coverage_taxonomy.normalize` so differently-glyphed
    spellings of the same item don't double up.
    """
    base = max(records, key=lambda r: len(json.dumps(r, ensure_ascii=False)))
    merged = json.loads(json.dumps(base, ensure_ascii=False))  # deep copy
    ordered = [base] + [r for r in records if r is not base]
    for field in _UNION_FIELDS:
        seen: dict[str, str] = {}
        for rec in ordered:
            for item in (rec.get(field) or []):
                if not isinstance(item, str):
                    continue
                key = coverage_taxonomy.normalize(item)
                if key and key not in seen:
                    seen[key] = item
        if seen:
            merged[field] = list(seen.values())
    return merged


def _sig_for(docs: list[dict], repeat: int, model: str, filter_tag: str) -> str:
    tag = f"|repeat={repeat}" if repeat > 1 else ""
    return (PROMPT_VERSION + f"|model={model}|filter={filter_tag}{tag}|"
            + "|".join(f"{d['doctype']}:{d['content_sha256']}" for d in docs))


# CHECK24 serves ONE document bundle for several tariffs — the 26 analyzed tariffs share
# only 15 distinct document sets. Since _sig_for() hashes the documents (plus prompt,
# model, filter, repeat) and nothing tariff-specific, those stems are literally the same
# extraction request. The per-stem cache in _process_stem cannot see that: it only ever
# looks at out/tariffs/<own stem>.json. So a full re-extract paid for 26 model runs where
# 15 would do — with --repeat 3, 78 calls instead of 45.
#
# Reusing the twin's facts is not a shortcut but the more honest result: with identical
# inputs any difference between the two records is model variance, not a product
# difference the documents could ever justify.
_SHARED_LOCK = threading.Lock()
_SHARED_BY_HASH: dict[str, tuple[str, dict]] = {}  # input hash -> (source stem, record)


def _is_degenerate(stem: str, rec: dict) -> bool:
    """Does this record just echo its own stem instead of carrying extracted facts?

    A real extraction reads insurer and tariff out of the documents ("ARAG SE",
    "Aktiv-Rechtsschutz für Privatpersonen Premium"). A record whose identity equals the
    slug it is filed under never got that far — arag__premium-familienrecht-2026 sits at
    insurer='arag', tariff='premium-familienrecht-2026'. Such a record must not seed the
    twin index: cloning it spreads a failed extraction to every stem sharing its
    documents, turning one bad record into several.

    BOTH halves must echo the slug. A tariff name alone legitimately matches its slug
    ("Klaro" -> dmb__klaro, "JURPRIVAT" -> ks-auxilia__jurprivat) while the insurer
    field carries a proper extracted name — requiring only one match would reject those
    healthy records as sources.
    """
    insurer_slug, _, tariff_slug = stem.partition("__")
    return (slug(str(rec.get("insurer") or "")) == insurer_slug
            and slug(str(rec.get("tariff") or "")) == tariff_slug)


def _seed_shared_index() -> None:
    """Index already-extracted records by input hash so a stem whose twin is on disk
    reuses it instead of re-extracting. Skipped under --force, which must re-run the
    model for real."""
    for path in sorted(OUT.glob("*.json")):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        h = rec.get("_input_hash")
        # A curated record carries hand-verified patches for ITS tariff; cloning those
        # onto a twin would silently spread an edit that was never verified there.
        if h and not rec.get("_curated") and not _is_degenerate(path.stem, rec):
            _SHARED_BY_HASH.setdefault(h, (path.stem, rec))


def _process_stem(insurer: str, tariff: str, docs: list[dict], model: str, force: bool,
                  repeat: int, filter_tag: str, schema_text: str,
                  transform) -> tuple[list[tuple[bool, str]], bool]:
    """Cache-check and (re)extract one tariff. Returns (lines, failed).

    `lines` is the ordered (is_stderr, text) output this stem produced; buffering it
    (instead of printing directly) keeps one stem's output contiguous when several
    stems run concurrently across --jobs worker threads — the caller prints each
    stem's lines as one block as soon as its future completes. `failed` marks a
    stem where every run produced no usable record (rc=1 case)."""
    stem = f"{slug(insurer)}__{slug(tariff)}"
    lines: list[tuple[bool, str]] = []

    def emit(text: str, err: bool = False) -> None:
        lines.append((err, text))

    docs = sorted(docs, key=lambda d: d["doctype"])
    input_hash = hashlib.sha256(_sig_for(docs, repeat, model, filter_tag).encode()).hexdigest()
    out_path = OUT / f"{stem}.json"

    if out_path.exists() and not force:
        try:
            prev = json.loads(out_path.read_text(encoding="utf-8"))
            if prev.get("_input_hash") == input_hash:
                emit(f"  cached    {insurer} / {tariff}")
                _tally("cached")
                return lines, False
            # Keep a stored record extracted at an equal-or-higher repeat over the
            # same inputs — it is strictly better; a lower-repeat run must not clobber
            # it. Reconstruct what its hash would have been at its own repeat to be
            # sure the inputs (docs/model/filter/prompt) actually match.
            prev_repeat = prev.get("_repeat") or 1
            if prev_repeat >= repeat:
                prev_hash = hashlib.sha256(
                    _sig_for(docs, prev_repeat, model, filter_tag).encode()).hexdigest()
                if prev.get("_input_hash") == prev_hash:
                    emit(f"  cached    {insurer} / {tariff}  "
                         f"(kept repeat={prev_repeat} >= {repeat})")
                    _tally("cached")
                    return lines, False
            # Curated records carry hand-verified patches (e.g. golden-pinned
            # module.level, a corrected Selbstbeteiligung) that a re-extract would
            # silently clobber. Only --force may override this guard.
            if prev.get("_curated"):
                emit(f"  SKIPPED   {insurer} / {tariff} ({stem}): record is curated "
                     f"(_curated: true, carries hand-verified patches) — re-extracting "
                     f"would overwrite them; pass --force to override.", err=True)
                _tally("cached")  # no model call, existing record kept
                return lines, False
        except Exception:
            pass

    # A twin is another stem whose documents (and prompt/model/filter/repeat) hash
    # identically — the very same extraction request, so its facts are reusable verbatim.
    with _SHARED_LOCK:
        entry = _SHARED_BY_HASH.get(input_hash)
    shared_from = None
    if entry is not None and entry[0] != stem:
        shared_from, twin = entry
        record = json.loads(json.dumps(twin))  # deep copy: stems must not share objects
        record.pop("_curated", None)  # hand-verified for ITS tariff, not for this one
        emit(f"  shared    {insurer} / {tariff}  "
             f"(same documents as {shared_from} — no model call)")
        _tally("shared")
    else:
        payload = build_payload(schema_text, docs, ROOT, transform)

        rep = f", repeat={repeat}" if repeat > 1 else ""
        emit(f"  extract   {insurer} / {tariff}  ({len(payload)} chars, {model}{rep}) ...")
        runs: list[dict] = []
        last_err = None
        for n in range(repeat):
            rec, err, cost = extract_once(model, payload)
            _record_cost(cost)
            if rec is not None:
                runs.append(rec)
            else:
                last_err = err
                emit(f"    run {n + 1}/{repeat} failed: {err}", err=True)
        if not runs:
            emit(f"    FAILED: {last_err or 'all runs failed'}", err=True)
            _tally("failed")
            return lines, True
        record = merge_records(runs) if len(runs) > 1 else runs[0]
        _tally("extracted")
        if repeat > 1:
            record["_repeat"] = repeat
            record["_repeat_ok"] = len(runs)

    record["_input_hash"] = input_hash
    record["_model"] = model
    record["_filter"] = filter_tag
    if shared_from:
        record["_shared_from"] = shared_from
    # Identity is known from the manifest — never let a model leave it null.
    record["insurer"] = record.get("insurer") or insurer
    record["tariff"] = record.get("tariff") or tariff
    # Provenance is the pipeline's job: set the real hashes authoritatively,
    # overwriting anything the model may have (wrongly) produced.
    record["sources"] = [{"doctype": d["doctype"],
                          "content_sha256": d["content_sha256"]} for d in docs]
    empty = [k for k in ("modules", "coverage") if not record.get(k)]
    if empty:
        emit(f"    warn: {insurer}/{tariff}: model returned empty {empty}", err=True)
    # Atomic write (tmp twin + os.replace): a crash — or a second pipeline run
    # racing on the same record — must never leave truncated/interleaved JSON
    # that load_all_details() would then silently drop. Each stem owns a distinct
    # out_path, so concurrent workers never race on the same file.
    tmp_out = out_path.with_suffix(".json.tmp")
    tmp_out.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_out, out_path)
    emit(f"    -> {out_path.relative_to(ROOT)}")
    if feature_history.archive_version(out_path.stem, record):
        emit(f"    -> history archived ({out_path.stem})")
    # Publish for the twins: a later stem over the same documents reuses this instead of
    # paying for an identical extraction. setdefault, so the first writer stays the
    # source and the attribution in _shared_from cannot flip mid-run. Same degeneracy
    # guard as the on-disk seed — a run that produced slug-echo identity must not
    # become the template for its twins.
    if not _is_degenerate(stem, record):
        with _SHARED_LOCK:
            _SHARED_BY_HASH.setdefault(input_hash, (stem, record))

    return lines, False


def _flush(lines: list[tuple[bool, str]]) -> None:
    """Print one stem's buffered lines as a contiguous block."""
    for err, text in lines:
        print(text, file=sys.stderr if err else sys.stdout)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="ignore cache, re-extract all")
    ap.add_argument("--model", default="claude",
                    help="model spec (default: claude CLI default; e.g. haiku, "
                         "opus, ollama:llama3.1:8b) — see scripts/_providers.py")
    ap.add_argument("--filter", action="store_true",
                    help="trim oversized AVBs to comparison-relevant passages so "
                         "small/cheap/local models fit them")
    ap.add_argument("--repeat", type=int, default=1, metavar="N",
                    help="extract each tariff N times and merge (union of "
                         "leistungen/ausschluesse) to damp run-to-run variance of "
                         "cheap models; default 1")
    ap.add_argument("--only", nargs="+", metavar="STEM", default=None,
                    help="restrict to these stems (insurer__tariff); useful with "
                         "--repeat to re-extract just a shortlist")
    ap.add_argument("--jobs", type=int, default=1, metavar="N",
                    help="extract this many tariffs concurrently via a bounded thread "
                         "pool (default: 1, sequential — identical to today's "
                         "behavior); mind provider rate limits, 2-4 is a reasonable "
                         "range for most APIs")
    args = ap.parse_args()

    if args.repeat < 1:
        print("error: --repeat must be >= 1", file=sys.stderr)
        return 2
    if args.jobs < 1:
        print("error: --jobs must be >= 1", file=sys.stderr)
        return 2
    only = set(args.only) if args.only else None

    transform = avb_transform if args.filter else None
    # Bake _filter.FILTER_VERSION into the cache tag so a heuristics change invalidates
    # cached --filter records. Compat clause: at version 1 the tag stays "avb-filter"
    # (byte-identical to before FILTER_VERSION existed) so this change alone does not
    # invalidate today's caches and trigger a silent paid re-extract of everything.
    filter_tag = ("avb-filter" if _filter.FILTER_VERSION == 1
                  else f"avb-filter-v{_filter.FILTER_VERSION}") if args.filter else "none"

    manifest_path = EXTRACTED / "manifest.json"
    if not manifest_path.exists():
        print("error: run scripts/ingest.py first (no manifest.json)", file=sys.stderr)
        return 2

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    schema_text = SCHEMA.read_text(encoding="utf-8")
    OUT.mkdir(parents=True, exist_ok=True)

    # Group documents by (insurer, tariff).
    tariffs: dict[tuple[str, str], list[dict]] = {}
    for d in manifest["documents"]:
        tariffs.setdefault((d["insurer"], d["tariff"]), []).append(d)

    # --repeat is part of the cache signature (a repeat=N record is keyed differently
    # from repeat=1). That means a plain repeat=1 run would otherwise see every
    # repeat>=2 record as a cache MISS and clobber it with a lower-recall re-extract
    # (this bit a deep-scan run after a reconcile). The cache check in _process_stem
    # guards against that: a stored record at an equal-or-higher repeat over the same
    # inputs is kept.

    # Under --force every stem must genuinely re-run the model, so the on-disk twins are
    # NOT seeded — within the run, the first stem of each document set still serves the
    # rest.
    if not args.force:
        _seed_shared_index()

    rc = 0
    seen_stems: set[str] = set()
    work: list[tuple[str, str, list[dict]]] = []
    for (insurer, tariff), docs in sorted(tariffs.items()):
        stem = f"{slug(insurer)}__{slug(tariff)}"
        seen_stems.add(stem)
        if only is not None and stem not in only:
            continue
        work.append((insurer, tariff, docs))

    if args.jobs <= 1:
        # Sequential fast path: byte-for-byte identical to pre-parallelization behavior
        # (same code as the worker, called in manifest order, output flushed immediately).
        for insurer, tariff, docs in work:
            lines, failed = _process_stem(insurer, tariff, docs, args.model, args.force,
                                          args.repeat, filter_tag, schema_text, transform)
            _flush(lines)
            if failed:
                rc = 1
    else:
        # Each stem owns a distinct out_path and does its own atomic write, so workers
        # never race on shared state; only the buffered print lines are consumed here,
        # in the (arbitrary) order futures complete.
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs)
        futs = {ex.submit(_process_stem, insurer, tariff, docs, args.model, args.force,
                          args.repeat, filter_tag, schema_text, transform)
                for insurer, tariff, docs in work}
        try:
            for fut in concurrent.futures.as_completed(futs):
                lines, failed = fut.result()
                _flush(lines)
                if failed:
                    rc = 1
        except KeyboardInterrupt:
            ex.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            ex.shutdown(wait=True)

    if only is not None:
        missing = sorted(only - seen_stems)
        if missing:
            print(f"warn: --only stem(s) not found in manifest: {', '.join(missing)}",
                  file=sys.stderr)
            rc = rc or 3

    # Run-cost sidecar for update_report.py. Overwritten each run: it describes THIS
    # invocation, while tmp/update-runs.jsonl (written by the report) accumulates.
    stats = dict(_RUN_STATS)
    stats["cost_usd"] = round(stats["cost_usd"], 6)
    cost_path = ROOT / "tmp" / "extract-cost.json"
    cost_path.parent.mkdir(parents=True, exist_ok=True)
    _jsonio.atomic_write_json(cost_path, {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "model": args.model, "filter": filter_tag, "repeat": args.repeat,
        **stats,
    })

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
