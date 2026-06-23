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

Requires the `claude` CLI on PATH. Pure stdlib — no uv deps needed:
Run:  python3 scripts/extract.py            (all tariffs, cached)
      python3 scripts/extract.py --force     (ignore cache)
      python3 scripts/extract.py --model opus
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXTRACTED = ROOT / "data" / "extracted"
SCHEMA = ROOT / "schema" / "tariff.schema.json"
OUT = ROOT / "out" / "tariffs"

# Bump when the prompt/schema semantics change to invalidate all caches.
PROMPT_VERSION = "1"

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
- For module `level`, use Basis/Komfort/Premium only if the documents grade the \
area; otherwise null.
- Keep array entries short (a few words each), in German."""


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n", "", t)
        t = re.sub(r"\n```\s*$", "", t)
    return t.strip()


def run_claude(payload: str, model: str | None) -> str:
    cmd = ["claude", "-p", INSTRUCTION]
    if model:
        cmd += ["--model", model]
    proc = subprocess.run(cmd, input=payload, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"claude -p failed (exit {proc.returncode}): {proc.stderr.strip()[:500]}")
    return proc.stdout


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="ignore cache, re-extract all")
    ap.add_argument("--model", default=None, help="claude model (default: CLI default)")
    args = ap.parse_args()

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
        sig = PROMPT_VERSION + "|" + "|".join(f"{d['doctype']}:{d['content_sha256']}" for d in docs)
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

        parts = [f"===== SCHEMA =====\n{schema_text}\n"]
        for d in docs:
            text = (ROOT / d["extracted_path"]).read_text(encoding="utf-8")
            parts.append(f"===== {d['doctype']} =====\n{text}\n")
        payload = "\n".join(parts)

        print(f"  extract   {insurer} / {tariff}  ({len(payload)} chars -> claude -p) ...")
        try:
            raw = run_claude(payload, args.model)
            record = json.loads(strip_fences(raw))
        except Exception as e:
            print(f"    FAILED: {e}", file=sys.stderr)
            rc = 1
            continue

        record["_input_hash"] = input_hash
        record["_model"] = args.model or "default"
        # Ensure provenance even if the model omitted it.
        record.setdefault("sources", [{"doctype": d["doctype"], "content_sha256": d["content_sha256"]} for d in docs])
        out_path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"    -> {out_path.relative_to(ROOT)}")

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
