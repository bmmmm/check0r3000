#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["jsonschema>=4"]
# ///
"""Eval stage: benchmark the extract prompt across models on three axes.

  A  Correctness  — schema-valid + hallucination guard + required-field plausibility
  B  Cost         — cost per call (from the provider; null for local models)
  C  Performance  — wall-clock + API duration + context-window fit

It runs the (tariff x model) matrix in parallel, scores every result against
ground truth derived from the Produktinformationsblätter, and prints a comparison
table. The same payload builder, instruction and providers as the real pipeline
are reused (imported from extract.py / _providers), so the benchmark scores exactly
what production runs — cloud (claude) and local (ollama:/mlx:) models side by side.

Cost and latency numbers are account- and time-specific, so results land in
tmp/eval/ (gitignored), not in tracked out/.

Run:  uv run scripts/eval.py
      uv run scripts/eval.py --models haiku,sonnet,opus
      uv run scripts/eval.py --models haiku --filter            # trimmed AVBs
      uv run scripts/eval.py --models haiku,ollama:llama3.1:8b  # cloud vs local
      uv run scripts/eval.py --rescore                          # offline re-score
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import extract  # noqa: E402  — reuse INSTRUCTION / build_payload / coerce_json / avb_transform
import _providers  # noqa: E402

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


def docs_text(docs: list[dict], transform=None) -> str:
    """Concatenated text of the given documents (the grounding source).

    Applies the same `transform` the payload used, so claims are grounded against
    exactly what the model was fed (e.g. the filtered AVB, not the full one).
    """
    parts = []
    for d in docs:
        t = (ROOT / d["extracted_path"]).read_text(encoding="utf-8")
        if transform:
            t = transform(d["doctype"], t)
        parts.append(t)
    return "\n".join(parts)


def safe_name(s: str) -> str:
    """Filesystem-safe token for a model spec (which may contain ':', '@', '/')."""
    return re.sub(r"[^A-Za-z0-9.+_-]", "_", s)


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
    # The model is not responsible for `sources` provenance — the pipeline injects
    # the real content hashes, which the model cannot know. Validate its output with
    # `sources` optional and ignored, so a missing/empty provenance is not a "fail".
    model_schema = dict(schema)
    model_schema["required"] = [r for r in schema.get("required", []) if r != "sources"]
    model_record = {k: v for k, v in record.items() if k != "sources"}
    validator = Draft202012Validator(model_schema)
    errs = sorted(validator.iter_errors(model_record), key=lambda e: list(e.path))
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
            schema: dict, transform=None, filter_tag: str = "none") -> dict:
    variant = "+".join(d["doctype"] for d in docs)
    source_text = docs_text(docs, transform)  # ground against what was actually fed
    payload = extract.build_payload(schema_text, docs, ROOT, transform)

    res = _providers.run(model, extract.INSTRUCTION, payload)
    base = {"tariff": tariff_key, "model": model, "variant": variant,
            "filter": filter_tag, "payload_chars": len(payload),
            "cost_usd": res["cost_usd"], "wall_s": res["wall_s"], "api_s": res["api_s"],
            "input_tokens": res["input_tokens"], "output_tokens": res["output_tokens"],
            "context_window": res["context_window"]}

    if res["error"] or not res["text"]:
        return {**base, "status": "error", "error": res["error"] or "empty response"}
    try:
        record = extract.coerce_json(res["text"])
    except json.JSONDecodeError as e:
        return {**base, "status": "error", "error": f"record not JSON: {e}"}

    EVAL_OUT.joinpath("records").mkdir(parents=True, exist_ok=True)
    rec_name = (f"{tariff_key.replace(' / ', '~')}~{variant}~{filter_tag}~"
                f"{safe_name(model)}.json")
    (EVAL_OUT / "records" / rec_name).write_text(
        json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

    return {**base, "status": "ok", **score(record, source_text, schema)}


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


def _input_label(r: dict) -> str:
    inp = (r.get("variant", "?").replace("produktinfoblatt", "pib")
           .replace("weitere_unterlagen", "weit"))
    if r.get("filter") and r["filter"] != "none":
        inp += "/filt"
    return inp[:12]


def print_table(results: list[dict]) -> None:
    print(f"\n{'tariff':<26} {'model':<20} {'input':<12} {'status':<6} {'cost':>8} "
          f"{'wall_s':>7} {'in_tok':>8} {'ctx':>6} {'schema':>6} {'halluc':>6} {'faith':>6}")
    print("-" * 122)
    for r in sorted(results, key=lambda x: (x["tariff"], x["model"], _input_label(x))):
        cost = f"${r['cost_usd']:.3f}" if r.get("cost_usd") is not None else "-"
        intok = r.get("input_tokens")
        ctx = r.get("context_window")
        schema = "ok" if r.get("schema_valid") else ("FAIL" if r["status"] == "ok" else "-")
        faith = "YES" if r.get("faithful") else ("no" if r["status"] == "ok" else "-")
        print(f"{r['tariff']:<26} {r['model']:<20} {_input_label(r):<12} {r['status']:<6} "
              f"{cost:>8} {str(r.get('wall_s', '-')):>7} "
              f"{(intok if intok is not None else '-'):>8} "
              f"{(f'{ctx // 1000}k' if ctx else '-'):>6} {schema:>6} "
              f"{str(r.get('unsupported_claims', '-')):>6} {faith:>6}")
        if r["status"] == "error":
            print(f"    └─ {r['error']}")
        elif r.get("hallucinations"):
            for h in r["hallucinations"]:
                tag = "grounded" if h["grounded"] else "HALLUCINATION (not in fed source)"
                print(f"    └─ {h['field']} = {h['value']!r}  [{tag}]")


def rescore(docs_by_tariff: dict, schema: dict) -> list[dict]:
    """Re-score saved records offline (no API), grounding against the docs each fed."""
    results = []
    for f in sorted((EVAL_OUT / "records").glob("*.json")):
        if f.stem.count("~") < 4:
            print(f"  skip {f.name}: unrecognized name format")
            continue
        keypart, variant, filter_tag, model = f.stem.rsplit("~", 3)
        tariff_key = keypart.replace("~", " / ")
        if tariff_key not in docs_by_tariff:
            print(f"  skip {f.name}: no docs for '{tariff_key}'")
            continue
        fed_types = set(variant.split("+"))
        transform = extract.avb_transform if filter_tag != "none" else None
        fed = [d for d in docs_by_tariff[tariff_key] if d["doctype"] in fed_types]
        record = json.loads(f.read_text(encoding="utf-8"))
        results.append({"tariff": tariff_key, "model": model, "status": "ok",
                        "variant": variant, "filter": filter_tag,
                        **score(record, docs_text(fed, transform), schema)})
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
    ap.add_argument("--filter", action="store_true",
                    help="trim oversized AVBs to comparison-relevant passages "
                         "(see scripts/_filter.py)")
    ap.add_argument("--rescore", action="store_true",
                    help="re-score saved records in tmp/eval/records (offline, no API)")
    args = ap.parse_args()
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    doc_filter = {d.strip() for d in args.docs.split(",")} if args.docs else None
    transform = extract.avb_transform if args.filter else None
    filter_tag = "avb-filter" if args.filter else "none"

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
          f"docs={docs_label}, filter={'on' if args.filter else 'off'}, in parallel.")
    for key in sorted(feeds):
        approx = len(extract.build_payload(schema_text, feeds[key], ROOT, transform)) // 4
        kinds = ",".join(d["doctype"] for d in feeds[key])
        print(f"  {key}: ~{approx // 1000}k tokens payload [{kinds}]")
    print()

    with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as ex:
        futures = [ex.submit(run_job, key, docs, m, schema_text, schema, transform, filter_tag)
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
