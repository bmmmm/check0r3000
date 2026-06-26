#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
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
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _filter  # noqa: E402
import _providers  # noqa: E402
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


def avb_transform(doctype: str, text: str) -> str:
    """Trim only the AVB (the oversized document); pass others through unchanged."""
    return _filter.filter_text(text) if doctype == "avb" else text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="ignore cache, re-extract all")
    ap.add_argument("--model", default="claude",
                    help="model spec (default: claude CLI default; e.g. haiku, "
                         "opus, ollama:llama3.1:8b) — see scripts/_providers.py")
    ap.add_argument("--filter", action="store_true",
                    help="trim oversized AVBs to comparison-relevant passages so "
                         "small/cheap/local models fit them")
    args = ap.parse_args()

    transform = avb_transform if args.filter else None
    filter_tag = "avb-filter" if args.filter else "none"

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

    rc = 0
    for (insurer, tariff), docs in sorted(tariffs.items()):
        docs = sorted(docs, key=lambda d: d["doctype"])
        sig = (PROMPT_VERSION + f"|model={args.model}|filter={filter_tag}|"
               + "|".join(f"{d['doctype']}:{d['content_sha256']}" for d in docs))
        input_hash = hashlib.sha256(sig.encode()).hexdigest()
        out_path = OUT / f"{slug(insurer)}__{slug(tariff)}.json"

        if out_path.exists() and not args.force:
            try:
                prev = json.loads(out_path.read_text(encoding="utf-8"))
                if prev.get("_input_hash") == input_hash:
                    print(f"  cached    {insurer} / {tariff}")
                    continue
            except Exception:
                pass

        payload = build_payload(schema_text, docs, ROOT, transform)

        print(f"  extract   {insurer} / {tariff}  ({len(payload)} chars, {args.model}) ...")
        result = _providers.run(args.model, INSTRUCTION, payload)
        if result["error"] or not result["text"]:
            print(f"    FAILED: {result['error'] or 'empty response'}", file=sys.stderr)
            rc = 1
            continue
        try:
            record = coerce_json(result["text"])
        except Exception as e:
            print(f"    FAILED: could not parse JSON: {e}", file=sys.stderr)
            rc = 1
            continue

        record["_input_hash"] = input_hash
        record["_model"] = args.model
        record["_filter"] = filter_tag
        # Identity is known from the manifest — never let a model leave it null.
        record["insurer"] = record.get("insurer") or insurer
        record["tariff"] = record.get("tariff") or tariff
        # Provenance is the pipeline's job: set the real hashes authoritatively,
        # overwriting anything the model may have (wrongly) produced.
        record["sources"] = [{"doctype": d["doctype"],
                              "content_sha256": d["content_sha256"]} for d in docs]
        empty = [k for k in ("modules", "coverage") if not record.get(k)]
        if empty:
            print(f"    warn: {insurer}/{tariff}: model returned empty {empty}", file=sys.stderr)
        out_path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"    -> {out_path.relative_to(ROOT)}")
        if feature_history.archive_version(out_path.stem, record):
            print(f"    -> history archived ({out_path.stem})")

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
