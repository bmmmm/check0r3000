#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["jsonschema>=4"]
# ///
"""Eval stage: benchmark the extract prompt across models on three axes.

  A  Correctness  — schema-valid + hallucination guard + required-field plausibility
  B  Cost         — total_cost_usd per call (from `claude -p --output-format json`)
  C  Performance  — wall-clock + API duration + context-window fit

It runs the (tariff x model) matrix in parallel, scores every result against
ground truth derived from the Produktinformationsblätter, and prints a comparison
table. The same payload builder and instruction as the real pipeline are reused
(imported from extract.py), so the benchmark scores exactly what production runs.

Cost and latency numbers are account- and time-specific, so results land in
tmp/eval/ (gitignored), not in tracked out/.

Run:  uv run scripts/eval.py
      uv run scripts/eval.py --models haiku,sonnet,opus
      uv run scripts/eval.py --models opus --tariff arag
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import extract  # noqa: E402  — reuse INSTRUCTION / build_payload / strip_fences / slug

EXTRACTED = ROOT / "data" / "extracted"
SCHEMA = ROOT / "schema" / "tariff.schema.json"
EVAL_OUT = ROOT / "tmp" / "eval"

# --- Ground truth from the Produktinformationsblätter (the consumer summary) ----
# Sum insured, deductible and premium are explicitly NOT stated as values in the
# documents ("entnehmen Sie Ihrem Versicherungsschein"). A faithful extraction
# MUST leave these null; any value is flagged and digit-checked against the source.
HALLUCINATION_GUARD = [
    ("coverage", "versicherungssumme"),
    ("coverage", "selbstbeteiligung"),
    ("beitrag", "monatlich_eur"),
    ("beitrag", "jaehrlich_eur"),
]
# Fields the documents clearly state -> must be populated (and, where given, match).
REQUIRE_PRESENT = {
    ("coverage", "geltungsbereich"): "europ",   # lowercased substring expected
    ("coverage", "vertragslaufzeit"): None,      # any non-null value
}
MODULE_KEYS = ["privat", "beruf", "verkehr", "wohnen_immobilien",
               "internet_web", "steuer", "sozialgericht", "verwaltungsrecht"]


def docs_text(docs: list[dict]) -> str:
    """Concatenated text of the given documents (the grounding source)."""
    return "\n".join((ROOT / d["extracted_path"]).read_text(encoding="utf-8") for d in docs)


def norm_digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def grounded(value, source_digits: str, source_low: str) -> bool:
    """True if the claimed value can be traced back to the source text.

    Each number in the value is checked individually (a value may carry several,
    e.g. '150/300 EUR'); a value with no numbers is matched as a lowercased
    substring. Single-digit runs are ignored as too noisy to anchor on.
    """
    s = str(value)
    nums = [norm_digits(n) for n in re.findall(r"\d[\d.\s]*\d|\d", s)]
    nums = [n for n in nums if len(n) >= 2]
    if nums:
        return all(n in source_digits for n in nums)
    return s.lower().strip() in source_low


def score(record: dict, source_text: str, schema: dict) -> dict:
    validator = Draft202012Validator(schema)
    errs = sorted(validator.iter_errors(record), key=lambda e: list(e.path))
    schema_errors = [f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errs[:5]]

    src_low = source_text.lower()
    src_digits = norm_digits(source_text)

    hallucinations = []
    for sec, field in HALLUCINATION_GUARD:
        val = (record.get(sec) or {}).get(field)
        if val not in (None, "", []):
            hallucinations.append({
                "field": f"{sec}.{field}", "value": val,
                "grounded": grounded(val, src_digits, src_low),
            })

    present = {}
    for (sec, field), needle in REQUIRE_PRESENT.items():
        val = (record.get(sec) or {}).get(field)
        ok = val not in (None, "", [])
        if ok and needle:
            ok = needle in str(val).lower()
        present[f"{sec}.{field}"] = ok

    mods = record.get("modules") or {}
    included = [k for k in MODULE_KEYS if (mods.get(k) or {}).get("included")]
    levels = {k: (mods.get(k) or {}).get("level") for k in included}

    unsupported = [h for h in hallucinations if not h["grounded"]]
    faithful = (not schema_errors) and (not unsupported) and all(present.values())

    return {
        "schema_valid": not schema_errors,
        "schema_errors": schema_errors,
        "hallucinations": hallucinations,
        "unsupported_claims": len(unsupported),
        "required_present": present,
        "modules_included": included,
        "modules_levels": levels,
        "faithful": faithful,
    }


def run_job(tariff_key: str, docs: list[dict], model: str, schema_text: str,
            schema: dict) -> dict:
    variant = "+".join(d["doctype"] for d in docs)
    source_text = docs_text(docs)  # ground claims against what was actually fed
    payload = extract.build_payload(schema_text, docs, ROOT)
    cmd = ["claude", "-p", extract.INSTRUCTION, "--model", model, "--output-format", "json"]
    t0 = time.monotonic()
    proc = subprocess.run(cmd, input=payload, capture_output=True, text=True)
    wall = time.monotonic() - t0

    base = {"tariff": tariff_key, "model": model, "payload_chars": len(payload),
            "wall_s": round(wall, 1)}

    if proc.returncode != 0:
        return {**base, "status": "error",
                "error": (proc.stderr or proc.stdout).strip()[:300] or f"exit {proc.returncode}"}
    try:
        outer = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {**base, "status": "error", "error": "claude output not JSON"}

    usage = outer.get("usage", {})
    model_usage = next(iter((outer.get("modelUsage") or {}).values()), {})
    meta = {
        "cost_usd": outer.get("total_cost_usd"),
        "api_s": round((outer.get("duration_ms") or 0) / 1000, 1),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_read": usage.get("cache_read_input_tokens"),
        "context_window": model_usage.get("contextWindow"),
    }

    if outer.get("is_error"):
        return {**base, **meta, "status": "error",
                "error": (outer.get("api_error_status") or str(outer.get("result")))[:300]}

    try:
        record = extract.coerce_json(outer.get("result", ""))
    except json.JSONDecodeError as e:
        return {**base, **meta, "status": "error", "error": f"record not JSON: {e}"}

    EVAL_OUT.joinpath("records").mkdir(parents=True, exist_ok=True)
    rec_name = f"{tariff_key.replace(' / ', '~')}~{variant}~{model}.json"
    (EVAL_OUT / "records" / rec_name).write_text(
        json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

    return {**base, **meta, "status": "ok", "variant": variant,
            **score(record, source_text, schema)}


def cross_model(results: list[dict]) -> dict:
    """Per tariff, surface where the successful models disagree."""
    out: dict[str, dict] = {}
    by_tariff: dict[str, list[dict]] = {}
    for r in results:
        if r["status"] == "ok":
            by_tariff.setdefault(r["tariff"], []).append(r)
    for tariff, recs in by_tariff.items():
        if len(recs) < 2:
            continue
        mod_sets = {r["model"]: set(r["modules_included"]) for r in recs}
        all_mods = set().union(*mod_sets.values())
        disagree = sorted(m for m in all_mods
                          if any(m in s for s in mod_sets.values())
                          and not all(m in s for s in mod_sets.values()))
        out[tariff] = {
            "models": [r["model"] for r in recs],
            "module_disagreements": {
                m: {model: (m in s) for model, s in mod_sets.items()} for m in disagree
            },
        }
    return out


def print_table(results: list[dict]) -> None:
    cols = ["tariff", "model", "status", "cost_usd", "wall_s", "api_s",
            "in_tok", "ctx", "schema", "halluc", "faithful"]
    print("\n" + "  ".join(f"{c:>9}" if c not in ("tariff",) else f"{c:<26}" for c in cols))
    print("-" * 120)
    for r in sorted(results, key=lambda x: (x["tariff"], x["model"])):
        cost = f"${r['cost_usd']:.3f}" if r.get("cost_usd") is not None else "-"
        intok = r.get("input_tokens")
        ctx = r.get("context_window")
        row = [
            f"{r['tariff']:<26}", f"{r['model']:>9}", f"{r['status']:>9}",
            f"{cost:>9}", f"{r.get('wall_s', '-'):>9}", f"{r.get('api_s', '-'):>9}",
            f"{intok if intok is not None else '-':>9}",
            f"{(f'{ctx//1000}k' if ctx else '-'):>9}",
            f"{('ok' if r.get('schema_valid') else 'FAIL' if r['status'] == 'ok' else '-'):>9}",
            f"{(r.get('unsupported_claims', '-')):>9}",
            f"{('YES' if r.get('faithful') else 'no' if r['status'] == 'ok' else '-'):>9}",
        ]
        print("  ".join(row))
        if r["status"] == "error":
            print(f"             └─ {r['error']}")
        elif r.get("hallucinations"):
            for h in r["hallucinations"]:
                tag = "grounded" if h["grounded"] else "HALLUCINATION (not in source)"
                print(f"             └─ {h['field']} = {h['value']!r}  [{tag}]")


def rescore(docs_by_tariff: dict, schema: dict) -> list[dict]:
    """Re-score saved records offline (no API), grounding against the docs each fed."""
    results = []
    for f in sorted((EVAL_OUT / "records").glob("*.json")):
        if f.stem.count("~") < 3:
            print(f"  skip {f.name}: unrecognized name format")
            continue
        keypart, variant, model = f.stem.rsplit("~", 2)
        tariff_key = keypart.replace("~", " / ")
        if tariff_key not in docs_by_tariff:
            print(f"  skip {f.name}: no docs for '{tariff_key}'")
            continue
        fed_types = set(variant.split("+"))
        fed = [d for d in docs_by_tariff[tariff_key] if d["doctype"] in fed_types]
        record = json.loads(f.read_text(encoding="utf-8"))
        results.append({"tariff": tariff_key, "model": model, "status": "ok",
                        "variant": variant, **score(record, docs_text(fed), schema)})
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description="Benchmark extract across models.")
    ap.add_argument("--models", default="haiku,sonnet,opus",
                    help="comma-separated model aliases (default: haiku,sonnet,opus)")
    ap.add_argument("--tariff", default=None,
                    help="restrict to tariffs whose insurer/slug contains this substring")
    ap.add_argument("--docs", default=None,
                    help="comma-separated doctypes to feed (default: all), e.g. "
                         "produktinfoblatt,weitere_unterlagen")
    ap.add_argument("--rescore", action="store_true",
                    help="re-score saved records in tmp/eval/records (offline, no API)")
    args = ap.parse_args()
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    doc_filter = {d.strip() for d in args.docs.split(",")} if args.docs else None

    manifest_path = EXTRACTED / "manifest.json"
    if not manifest_path.exists():
        print("error: run scripts/ingest.py first (no manifest.json)", file=sys.stderr)
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    schema_text = SCHEMA.read_text(encoding="utf-8")
    schema = json.loads(schema_text)

    tariffs: dict[str, list[dict]] = {}
    for d in manifest["documents"]:
        key = f"{d['insurer']} / {d['tariff']}"
        if args.tariff and args.tariff.lower() not in key.lower():
            continue
        tariffs.setdefault(key, []).append(d)
    if not tariffs:
        print("error: no tariffs matched", file=sys.stderr)
        return 2

    if args.rescore:
        print("Re-scoring saved records offline (no API calls):")
        results = rescore(tariffs, schema)
        print_table(results)
        cm = cross_model(results)
        if cm:
            print("\nCross-model disagreements (modules):")
            for tariff, info in cm.items():
                print(f"  {tariff}: " + ("agree" if not info["module_disagreements"]
                      else ", ".join(info["module_disagreements"])))
        return 0

    # Build the fed-document subset (--docs); sources for grounding stay full.
    feeds = {}
    for key, docs in sorted(tariffs.items()):
        feed = [d for d in docs if not doc_filter or d["doctype"] in doc_filter]
        feeds[key] = sorted(feed, key=lambda d: d["doctype"])

    jobs = [(key, feeds[key], m) for key in sorted(feeds) for m in models if feeds[key]]
    docs_label = args.docs or "all"
    print(f"Running {len(jobs)} job(s): {len(feeds)} tariff(s) x {len(models)} model(s), "
          f"docs={docs_label}, in parallel.")
    for key in sorted(feeds):
        approx = len(extract.build_payload(schema_text, feeds[key], ROOT)) // 4
        kinds = ",".join(d["doctype"] for d in feeds[key])
        print(f"  {key}: ~{approx // 1000}k tokens payload [{kinds}]")
    print()

    with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as ex:
        futures = [ex.submit(run_job, key, docs, m, schema_text, schema)
                   for key, docs, m in jobs]
        results = [f.result() for f in futures]

    print_table(results)

    cm = cross_model(results)
    if cm:
        print("\nCross-model disagreements (modules):")
        for tariff, info in cm.items():
            if info["module_disagreements"]:
                print(f"  {tariff}  ({', '.join(info['models'])})")
                for mod, votes in info["module_disagreements"].items():
                    print(f"    {mod}: " + ", ".join(f"{m}={'Y' if v else 'n'}" for m, v in votes.items()))
            else:
                print(f"  {tariff}: modules agree across {', '.join(info['models'])}")

    EVAL_OUT.mkdir(parents=True, exist_ok=True)
    (EVAL_OUT / "results.json").write_text(
        json.dumps({"jobs": results, "cross_model": cm}, indent=2, ensure_ascii=False),
        encoding="utf-8")
    print(f"\nFull results -> {(EVAL_OUT / 'results.json').relative_to(ROOT)}")
    print(f"Per-record extractions -> {(EVAL_OUT / 'records').relative_to(ROOT)}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
