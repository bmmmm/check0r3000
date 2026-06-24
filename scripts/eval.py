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
import datetime
import json
import re
import subprocess
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
# Durable, committable benchmark digest (correctness is reproducible; cost/latency
# are indicative snapshots). The full per-run records stay in tmp/eval (gitignored).
BENCH_OUT = ROOT / "benchmarks"

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
            schema: dict, transform=None, filter_tag: str = "none",
            run_idx: int = 0, n_repeat: int = 1) -> dict:
    variant = "+".join(d["doctype"] for d in docs)
    source_text = docs_text(docs, transform)  # ground against what was actually fed
    payload = extract.build_payload(schema_text, docs, ROOT, transform)

    res = _providers.run(model, extract.INSTRUCTION, payload)
    base = {"tariff": tariff_key, "model": model, "variant": variant, "run": run_idx,
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
    # Keep the 4-segment name parseable by rescore(); distinguish repeat runs by
    # suffixing the model token only (rescore treats it as just a label).
    token = safe_name(model) + (f"__r{run_idx}" if n_repeat > 1 else "")
    rec_name = f"{tariff_key.replace(' / ', '~')}~{variant}~{filter_tag}~{token}.json"
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
        # Repeat runs are saved as `<model>__r{idx}`; recover the plain model alias
        # (and the run index) so offline aggregation/variance groups them as one model.
        run_idx = 0
        mr = re.search(r"__r(\d+)$", model)
        if mr:
            run_idx, model = int(mr.group(1)), model[:mr.start()]
        fed_types = set(variant.split("+"))
        transform = extract.avb_transform if filter_tag != "none" else None
        fed = [d for d in docs_by_tariff[tariff_key] if d["doctype"] in fed_types]
        record = json.loads(f.read_text(encoding="utf-8"))
        results.append({"tariff": tariff_key, "model": model, "status": "ok",
                        "variant": variant, "filter": filter_tag, "run": run_idx,
                        **score(record, docs_text(fed, transform), schema)})
    return results


def aggregate(results: list[dict]) -> list[dict]:
    """Collapse repeat runs into one row per (tariff, model, input).

    Faithful/schema counts are over all runs (an errored run is a failure);
    module spread and cost/latency means are over the successful runs.
    """
    # Group on the RAW (variant, filter), not the truncated display label, so
    # filtered and unfiltered runs of a long-variant tariff never collide.
    groups: dict[tuple, list[dict]] = {}
    for r in results:
        key = (r["tariff"], r["model"], r.get("variant", ""), r.get("filter", "none"))
        groups.setdefault(key, []).append(r)
    rows = []
    for (tariff, model, _variant, _filter), runs in sorted(groups.items()):
        inp = _input_label(runs[0])
        ok = [r for r in runs if r["status"] == "ok"]
        mods = [len(r.get("modules_included", [])) for r in ok]
        costs = [r["cost_usd"] for r in runs if r.get("cost_usd") is not None]
        walls = [r["wall_s"] for r in runs if isinstance(r.get("wall_s"), (int, float))]
        rows.append({
            "tariff": tariff, "model": model, "input": inp,
            "runs": len(runs), "ok": len(ok),
            "schema_ok": sum(1 for r in ok if r.get("schema_valid")),
            "faithful": sum(1 for r in ok if r.get("faithful")),
            "modules_min": min(mods) if mods else 0,
            "modules_max": max(mods) if mods else 0,
            "unsupported_max": max((r.get("unsupported_claims", 0) for r in ok), default=0),
            "cost_usd": round(sum(costs) / len(costs), 4) if costs else None,
            "wall_s": round(sum(walls) / len(walls), 1) if walls else None,
        })
    return rows


def _modules_cell(r: dict) -> str:
    return (str(r["modules_max"]) if r["modules_min"] == r["modules_max"]
            else f"{r['modules_min']}-{r['modules_max']}")


def print_variance(rows: list[dict]) -> None:
    """Per (tariff, model, input): how stable repeated runs are (the key risk of
    cheap, non-deterministic models)."""
    print(f"\nVariance over repeated runs (faithful = grounded & complete):")
    print(f"{'tariff':<26} {'model':<18} {'input':<12} {'runs':>4} "
          f"{'schema':>7} {'faith':>7} {'modules':>8} {'~cost':>7}")
    print("-" * 94)
    for r in rows:
        cost = f"${r['cost_usd']:.3f}" if r["cost_usd"] is not None else "-"
        schema = f"{r['schema_ok']}/{r['runs']}"
        faith = f"{r['faithful']}/{r['runs']}"
        print(f"{r['tariff']:<26} {r['model']:<18} {r['input']:<12} {r['runs']:>4} "
              f"{schema:>7} {faith:>7} {_modules_cell(r):>8} {cost:>7}")


def _git_rev() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True, timeout=5).stdout.strip() or "?"
    except Exception:
        return "?"


def save_summary(rows: list[dict], models: list[str], repeat: int) -> None:
    """Write a durable, committable digest to benchmarks/ (correctness reproducible;
    cost/latency are indicative snapshots, raw per-run records stay in tmp/eval)."""
    BENCH_OUT.mkdir(parents=True, exist_ok=True)
    date = datetime.date.today().isoformat()
    rev = _git_rev()

    lines = ["# Benchmark — Extraktionsmodelle", "",
             f"_Snapshot {date}, commit `{rev}`, {repeat} Lauf/Läufe je Zelle. "
             "**Korrektheit** (Schema/Faithful/Module) ist reproduzierbar; "
             "**Kosten/Latenz** sind konto- und laufzeitspezifische Momentaufnahmen — "
             "nur als Größenordnung lesen. Rohdaten je Lauf: `tmp/eval/` (gitignored)._",
             "",
             "| Tarif | Modell | Input | Läufe | Schema | Faithful | Module | Halluz. | ~Kosten | ~wall_s |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        cost = f"${r['cost_usd']:.3f}" if r["cost_usd"] is not None else "–"
        wall = f"{r['wall_s']:.1f}" if r["wall_s"] is not None else "–"
        lines.append(f"| {r['tariff']} | {r['model']} | {r['input']} | {r['runs']} | "
                     f"{r['schema_ok']}/{r['runs']} | {r['faithful']}/{r['runs']} | "
                     f"{_modules_cell(r)} | {r['unsupported_max']} | {cost} | {wall} |")
    lines += ["",
              "_Faithful = schema-valid **und** jeder behauptete Wert im gefütterten "
              "Quelltext belegt **und** Pflichtfelder gesetzt. Halluz. = max. Anzahl "
              "nicht belegbarer Wertbehauptungen pro Lauf. Das Grounding ist substring-/"
              "ziffern-basiert: ausführliche Paraphrasen ohne wörtliche Zahl können als "
              "'nicht belegt' markiert werden, obwohl korrekt (eher bei reicheren "
              "Modell-Antworten) — als robusteres Signal `regression.py` gegen die "
              "dokument-gegroundeten Invarianten aus `golden.json` nutzen._", ""]

    (BENCH_OUT / "results.md").write_text("\n".join(lines), encoding="utf-8")
    (BENCH_OUT / "results.json").write_text(json.dumps(
        {"generated": date, "commit": rev, "models": models, "repeat": repeat,
         "rows": rows}, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nDurable summary -> {(BENCH_OUT / 'results.md').relative_to(ROOT)}"
          f"  +  {(BENCH_OUT / 'results.json').name}")


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
    ap.add_argument("--repeat", type=int, default=1,
                    help="run each (tariff x model) job N times to measure run-to-run "
                         "variance (cheap models drift in completeness)")
    ap.add_argument("--save-summary", action="store_true",
                    help="write a durable digest to benchmarks/ (results.md + .json) "
                         "for tracking model quality over time")
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
        rows = aggregate(results)
        if any(r["runs"] > 1 for r in rows):
            print_variance(rows)
        if args.save_summary:
            save_summary(rows, sorted({r["model"] for r in rows}),
                         max((r["runs"] for r in rows), default=1))
        return 0

    # Build the fed-document subset (--docs); sources for grounding stay full.
    feeds = {}
    for key, docs in sorted(tariffs.items()):
        feed = [d for d in docs if not doc_filter or d["doctype"] in doc_filter]
        feeds[key] = sorted(feed, key=lambda d: d["doctype"])

    repeat = max(1, args.repeat)
    jobs = [(key, feeds[key], m, r) for key in sorted(feeds) for m in models
            for r in range(repeat) if feeds[key]]
    docs_label = args.docs or "all"
    print(f"Running {len(jobs)} job(s): {len(feeds)} tariff(s) x {len(models)} model(s) "
          f"x {repeat} run(s), docs={docs_label}, filter={'on' if args.filter else 'off'}, "
          f"in parallel.")
    for key in sorted(feeds):
        approx = len(extract.build_payload(schema_text, feeds[key], ROOT, transform)) // 4
        kinds = ",".join(d["doctype"] for d in feeds[key])
        print(f"  {key}: ~{approx // 1000}k tokens payload [{kinds}]")
    print()

    with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as ex:
        futures = [ex.submit(run_job, key, docs, m, schema_text, schema, transform,
                             filter_tag, r, repeat)
                   for key, docs, m, r in jobs]
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

    rows = aggregate(results)
    if repeat > 1:
        print_variance(rows)
    if args.save_summary:
        save_summary(rows, models, repeat)

    EVAL_OUT.mkdir(parents=True, exist_ok=True)
    (EVAL_OUT / "results.json").write_text(
        json.dumps({"jobs": results, "cross_model": cm}, indent=2, ensure_ascii=False),
        encoding="utf-8")
    print(f"\nFull results -> {(EVAL_OUT / 'results.json').relative_to(ROOT)}")
    print(f"Per-record extractions -> {(EVAL_OUT / 'records').relative_to(ROOT)}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
