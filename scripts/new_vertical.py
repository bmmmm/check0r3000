#!/usr/bin/env python3
"""Scaffold a new insurance vertical from feasibility-probe evidence (Phase-D tool).

Takes the new vertical's identity (name/label/host/funnel path), 1-2 example AVB
texts and a result-page row dump (both produced by the Phase-A probe), and derives
DRAFTS via one model call: the schema `modules` block + labels, filter anchors, a
domain-adapted extraction instruction, a taxonomy skeleton and the query lever map.
Everything lands with an explicit `"_draft": true` marker; the registry entry starts
as `experimental`. Curation (refining the taxonomy, hand-verifying golden.json,
external ratings) stays human work and is the road from experimental to production.

Writes (refusing to touch an existing vertical without --force):
    schema/<v>/{tariff,offer}.schema.json
    config/verticals/<v>/{vertical.json, coverage_taxonomy.json,
                          magic-weights.json, needs-weights.json,
                          check24-profile.example.json}
    config/verticals.json   (registry entry, status experimental)

Run (config/ is write-denied in the Claude sandbox — run unsandboxed):
    python3 scripts/new_vertical.py --name hausrat --label Hausrat \\
        --host https://hausratversicherungen.check24.de \\
        --funnel-path /hausrat/vergleichsergebnis/ \\
        --avb tmp/vertical-probe/t3_hausrat_avb.txt \\
        --result-rows tmp/vertical-probe/hausrat.rows.json \\
        --result-url "https://.../vergleichsergebnis/?squaremeter=80&..." \\
        --model haiku
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _providers  # noqa: E402
import _vertical  # noqa: E402
from _jsonio import atomic_write_json  # noqa: E402
from extract import coerce_json  # noqa: E402

RS = "rechtsschutz"  # the worked example every draft is derived from
AVB_CAP = 120_000    # chars of AVB text handed to the model per document

INSTRUCTION = """You are designing the extraction configuration for a NEW German \
insurance vertical in an existing comparison tool. You get the WORKED EXAMPLE of the \
existing vertical (Rechtsschutz), plus evidence for the new vertical: example \
AVB text and scraped result-page rows.

Output ONLY one JSON object (no prose, no fences) with exactly these keys:
- "modules": object of 6-10 snake_case module keys -> one-line German description; \
these are the comparable coverage building blocks (Bausteine) of the new vertical, \
derived from the AVB evidence, in a sensible display order.
- "module_labels": object key -> short German display label (<= 20 chars).
- "filter_anchors": array of 18-30 lowercase regex fragments (like the worked \
example) matching the clauses a comparison cares about in this vertical's AVBs — \
include the module topics, deductible/sum/duration terms and exclusion markers.
- "extract_instruction": the full extraction prompt for this vertical, following the \
worked example's structure and rules but domain-adapted (name the vertical, drop \
rules that only make sense for Rechtsschutz, keep the facts-only/never-guess/null \
discipline and the German-short-entries rule verbatim in spirit).
- "query": {"pin_keys": array of query params from the result URL that pin the list \
to one insurer/tariff (empty array if none are evident), "module_labels": object \
mapping the coverage-relevant query params to short German labels}.
- "regression_generic_tokens": array of 2-6 lowercase tokens that appear in nearly \
every document FILENAME of this vertical but carry no product identity (the vertical \
name and its obvious word parts).
- "taxonomy": {"benefit_categories": array of 12-20 objects {key, label_de, kind: \
"leistung", synonyms: array of 3-8 lowercase strings}, "exclusion_categories": array \
of 5-10 objects {key, label_de, kind: "ausschluss", synonyms: [...]}} — canonical \
categories for lining up differently-worded benefits/exclusions across insurers, \
grounded in the AVB evidence.

Facts only — derive from the evidence, never invent insurer-specific numbers."""


def pdf_or_text(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader
        return "\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages)
    return path.read_text(encoding="utf-8")


def build_payload(args, rows: list[dict], query_pairs: list[tuple[str, str]]) -> str:
    rs_cfg = _vertical.vertical_config(RS)
    rs_tax = json.loads(_vertical.taxonomy_path(RS).read_text(encoding="utf-8"))
    rs_schema = json.loads(_vertical.tariff_schema_path(RS).read_text(encoding="utf-8"))
    example = {
        "modules": list(rs_schema["properties"]["modules"]["properties"]),
        "module_labels": rs_cfg.get("module_labels"),
        "filter_anchors": rs_cfg.get("filter_anchors"),
        "extract_instruction": rs_cfg.get("extract_instruction"),
        "query": rs_cfg.get("query"),
        "regression_generic_tokens": rs_cfg.get("regression_generic_tokens"),
        "taxonomy_sample": {
            "benefit_categories": rs_tax["benefit_categories"][:2],
            "exclusion_categories": rs_tax["exclusion_categories"][:1],
        },
    }
    parts = [
        f"===== WORKED EXAMPLE (vertical: Rechtsschutz) =====\n"
        f"{json.dumps(example, ensure_ascii=False, indent=1)}\n",
        f"===== NEW VERTICAL =====\nname: {args.name}\nlabel: {args.label}\n"
        f"host: {args.host}\n",
    ]
    if query_pairs:
        parts.append("===== RESULT-URL QUERY PARAMS =====\n"
                     + "\n".join(f"{k}={v}" for k, v in query_pairs) + "\n")
    if rows:
        parts.append("===== RESULT-PAGE ROWS (sample) =====\n"
                     + json.dumps(rows[:15], ensure_ascii=False, indent=1) + "\n")
    for i, avb in enumerate(args.avb):
        text = pdf_or_text(Path(avb))[:AVB_CAP]
        parts.append(f"===== AVB EXAMPLE {i + 1} =====\n{text}\n")
    return "\n".join(parts)


def validate_draft(d: dict) -> list[str]:
    errs = []
    mods = d.get("modules")
    if not isinstance(mods, dict) or not (3 <= len(mods) <= 12):
        errs.append(f"modules: expected 3-12 keys, got {mods if not isinstance(mods, dict) else len(mods)}")
    elif any(not re.fullmatch(r"[a-z][a-z0-9_]*", k) for k in mods):
        errs.append("modules: keys must be snake_case")
    if not isinstance(d.get("extract_instruction"), str) or len(d.get("extract_instruction", "")) < 200:
        errs.append("extract_instruction missing/too short")
    if not isinstance(d.get("filter_anchors"), list) or len(d.get("filter_anchors", [])) < 10:
        errs.append("filter_anchors missing/too few")
    for a in d.get("filter_anchors") or []:
        try:
            re.compile(a)
        except re.error as exc:
            errs.append(f"filter_anchors: invalid regex {a!r} ({exc})")
    tax = d.get("taxonomy") or {}
    for part, lo in (("benefit_categories", 8), ("exclusion_categories", 3)):
        cats = tax.get(part)
        if not isinstance(cats, list) or len(cats) < lo:
            errs.append(f"taxonomy.{part}: expected >= {lo} categories")
    return errs


def write_vertical(args, draft: dict, query_pairs: list[tuple[str, str]]) -> None:
    v = args.name
    rs_schema = json.loads(_vertical.tariff_schema_path(RS).read_text(encoding="utf-8"))
    schema = json.loads(json.dumps(rs_schema))  # deep copy
    schema["_draft"] = True
    schema["$id"] = rs_schema["$id"].replace("/schema/", f"/schema/{v}/")
    schema["title"] = f"{args.label} tariff comparison record (DRAFT)"
    schema["description"] = (f"Scaffolded draft for the {args.label} vertical — module "
                             "block model-derived from example AVBs; curate before "
                             "promoting the vertical to production.")
    schema["properties"]["modules"]["properties"] = {
        k: {"$ref": "#/$defs/module"} for k in draft["modules"]
    }
    # Draft tolerance: an un-curated vertical's model runs legitimately answer
    # `included: null` for optional add-on modules the documents only offer as
    # selectable extras. Curation tightens this back to a strict boolean when the
    # vertical is promoted to production.
    schema["$defs"]["module"]["properties"]["included"]["type"] = ["boolean", "null"]
    sdir = _vertical.schema_dir(v)
    sdir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(_vertical.tariff_schema_path(v), schema)
    offer = json.loads(_vertical.offer_schema_path(RS).read_text(encoding="utf-8"))
    offer["_draft"] = True
    atomic_write_json(_vertical.offer_schema_path(v), offer)

    cdir = _vertical.config_dir(v)
    cdir.mkdir(parents=True, exist_ok=True)
    labels = draft.get("module_labels") or {}
    atomic_write_json(_vertical.vertical_json_path(v), {
        "_draft": True,
        "_comment": ("Scaffolded by new_vertical.py from probe evidence — curate "
                     "before production."
                     + ((" Module descriptions from the draft: "
                         + "; ".join(f"{k}: {d}" for k, d in draft["modules"].items() if d))
                        if any(draft["modules"].values()) else "")),
        "label": args.label,
        "module_labels": {k: str(labels.get(k) or k) for k in draft["modules"]},
        "filter_anchors": draft["filter_anchors"],
        "extract_instruction": draft["extract_instruction"],
        "query": {
            "pin_keys": (draft.get("query") or {}).get("pin_keys") or [],
            "module_labels": (draft.get("query") or {}).get("module_labels") or {},
        },
        "regression_generic_tokens": draft.get("regression_generic_tokens") or [],
    })
    tax = draft["taxonomy"]
    atomic_write_json(_vertical.taxonomy_path(v), {
        "version": 1,
        "_draft": True,
        "_doc": (f"DRAFT taxonomy for {args.label}, model-derived from example AVBs. "
                 "Category order is match precedence; extend/curate by hand."),
        "benefit_categories": tax["benefit_categories"],
        "exclusion_categories": tax["exclusion_categories"],
    })
    atomic_write_json(_vertical.magic_weights_path(v), {
        "_comment": ("Scaffolded neutral placeholder — code defaults apply; see "
                     "MagicWeights in scripts/magic.py for the knobs."),
    })
    atomic_write_json(_vertical.needs_weights_path(v), {
        "_comment": ("Personal Bedarf weighting per Baustein (neutral 1.0 = objective "
                     "ranking). Edited via the TUI [W] editor or by hand."),
        **{k: 1 for k in draft["modules"]},
    })
    if args.result_url:
        base = args.result_url.split("?", 1)[0]
        atomic_write_json(_vertical.profile_example_path(v), {
            "_note": ("Tracked placeholder from the feasibility probe (deliberately "
                      "fake personal values). Copy to check24-profile.json (gitignored) "
                      "and adjust the levers."),
            "base_url": base,
            "query": args.result_url.split("?", 1)[1] if "?" in args.result_url else "",
        })

    reg = json.loads(_vertical.REGISTRY_PATH.read_text(encoding="utf-8"))
    reg["verticals"][v] = {
        "label": args.label,
        "host": args.host,
        "funnel_path": args.funnel_path,
        "status": "experimental",
    }
    atomic_write_json(_vertical.REGISTRY_PATH, reg)


def main() -> int:
    ap = argparse.ArgumentParser(description="Scaffold a new vertical from probe evidence.")
    ap.add_argument("--name", required=True, help="vertical key (snake_case)")
    ap.add_argument("--label", required=True, help="display label (German)")
    ap.add_argument("--host", required=True, help="https://<subdomain>.check24.de")
    ap.add_argument("--funnel-path", required=True,
                    help="path of the GET-query result page on that host")
    ap.add_argument("--avb", nargs="+", required=True,
                    help="1-2 example AVB files (.txt or .pdf) from the probe")
    ap.add_argument("--result-rows", help="JSON file with scraped result rows (probe)")
    ap.add_argument("--result-url", help="a full working result URL (query = payload)")
    ap.add_argument("--model", default="haiku", help="model spec for the draft call")
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing vertical's scaffold")
    args = ap.parse_args()

    if not re.fullmatch(r"[a-z][a-z0-9_]*", args.name):
        sys.exit(f"--name must be snake_case, got {args.name!r}")
    reg = json.loads(_vertical.REGISTRY_PATH.read_text(encoding="utf-8"))
    if args.name in reg["verticals"] and not args.force:
        sys.exit(f"vertical {args.name!r} already registered — pass --force to re-scaffold.")

    rows = []
    if args.result_rows:
        rows = json.loads(Path(args.result_rows).read_text(encoding="utf-8"))
    query_pairs = (parse_qsl(urlsplit(args.result_url).query, keep_blank_values=True)
                   if args.result_url else [])

    payload = build_payload(args, rows, query_pairs)
    print(f"[{args.name}] drafting via {args.model} ({len(payload)} chars payload) ...")
    result = _providers.run(args.model, INSTRUCTION, payload)
    if result["error"] or not result["text"]:
        sys.exit(f"draft call failed: {result['error'] or 'empty response'}")
    draft = coerce_json(result["text"])
    # Normalize: the worked example shows the modules as a bare key list, so the
    # model may answer in that shape — accept both list-of-keys and key->description.
    if isinstance(draft.get("modules"), list):
        draft["modules"] = {str(k): "" for k in draft["modules"]}
    errs = validate_draft(draft)
    if errs:
        (Path("tmp") / f"new_vertical_{args.name}_draft.json").write_text(
            json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
        for e in errs:
            print(f"  DRAFT-FAIL: {e}", file=sys.stderr)
        sys.exit("draft did not validate — inspect tmp/new_vertical_*_draft.json and re-run.")

    write_vertical(args, draft, query_pairs)
    print(f"[{args.name}] cost_usd={result['cost_usd']}  modules={list(draft['modules'])}")
    print(f"[{args.name}] scaffolded (status: experimental, all files marked _draft).")
    print("Next: harvest a few tariffs' documents into the manifest, download PDFs "
          "into the vertical's raw/, then CHECK0R_VERTICAL="
          f"{args.name} ingest -> extract -> regression.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
