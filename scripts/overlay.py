#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "jsonschema>=4",
# ]
# ///
"""Overlay stage: merge structured, NON-LLM facts onto the pure extraction records.

The premium (`beitrag`), the chosen service level per module (`modules.<m>.level`)
and the chosen `coverage.selbstbeteiligung` are NOT in the AVB/PIB PDFs, so the
extraction step correctly leaves them null (and benchmarks/golden.json pins them
null to prove the model never invents them). Those facts live in a check24 result
row, a personal Angebot, or a Leistungsuebersicht. This stage takes them from a
hand-authored, schema-validated structured file and merges them on top — no model
involved, every value copied verbatim from a file the user controls.

Reads:   out/tariffs/<key>.json        (pure LLM extraction — never modified)
         data/offers/<key>.json         (structured overlay; gitignored: personal)
         schema/offer.schema.json, schema/tariff.schema.json
Writes:  out/enriched/<key>.json        (merged; gitignored: carries the premium)

The pure records in out/tariffs/ stay frozen and regression.py keeps reading only
those, so the "the model never invents price/Stufe" guarantee can never be
contaminated by offer data. `out/enriched/` is a per-user, machine-regenerable
artifact — it carries the personal premium and MUST stay out of git (so does the
personal render render.py writes beside these records).

Run:  uv run scripts/overlay.py           # merge every data/offers/*.json
      uv run scripts/overlay.py --check    # re-validate existing out/enriched/, no merge
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent
TARIFFS = ROOT / "out" / "tariffs"
ENRICHED = ROOT / "out" / "enriched"
OFFERS = ROOT / "data" / "offers"
OFFER_SCHEMA = ROOT / "schema" / "offer.schema.json"
TARIFF_SCHEMA = ROOT / "schema" / "tariff.schema.json"

_MISSING = object()


def assert_path_ignored(probe: Path, what: str) -> None:
    """Refuse to run if `probe` is not git-ignored.

    out/enriched/ and the real data/offers/ files carry the personal premium/Stufe.
    out/ is a tracked deliverable directory and data/offers/ holds tracked
    template+README, so a routine `git add` could stage personal data. Verify the
    ignore rules are in place before any merge; a future .gitignore edit dropping
    one is then caught here, not in git history.
    """
    try:
        r = subprocess.run(["git", "check-ignore", "-q", str(probe)],
                           cwd=ROOT, capture_output=True)
    except FileNotFoundError:
        print(f"  note: git not found — cannot verify {what} is ignored; ensure "
              "'.gitignore' excludes it.", file=sys.stderr)
        return
    if r.returncode == 1:  # 0 = ignored, 1 = NOT ignored, 128 = not a repo
        print(f"error: {what} is NOT git-ignored. It carries the personal premium/\n"
              f"       Stufe from data/offers/ and must never be committed. Restore\n"
              f"       the .gitignore rule before running overlay.", file=sys.stderr)
        raise SystemExit(2)
    if r.returncode not in (0, 1):
        print(f"  note: 'git check-ignore' returned {r.returncode} (not a git repo?) "
              f"— cannot verify {what} is ignored.", file=sys.stderr)


def diff_paths(a, b, prefix: str = "") -> set[str]:
    """Dotted leaf paths where a and b differ (dict-aware; lists compared as values)."""
    if isinstance(a, dict) and isinstance(b, dict):
        changed: set[str] = set()
        for k in set(a) | set(b):
            sub = f"{prefix}.{k}" if prefix else k
            changed |= diff_paths(a.get(k, _MISSING), b.get(k, _MISSING), sub)
        return changed
    return set() if a == b else {prefix}


def comparable(record: dict) -> dict:
    """Strip provenance and bookkeeping so the containment diff sees only facts."""
    return {k: v for k, v in record.items()
            if k != "sources" and not k.startswith("_")}


def covered_by(path: str, allowed: list[str]) -> bool:
    """True if a changed leaf path falls under one of the allowed override prefixes.

    `allowed` is at the granularity {beitrag, modules.<m>.level, coverage.<k>}; a
    fully-replaced object (e.g. beitrag {nulls} -> {values}) yields leaf paths like
    beitrag.monatlich_eur, which this maps back to the allowed 'beitrag' prefix.
    """
    return any(path == a or path.startswith(a + ".") for a in allowed)


def is_empty_beitrag(b) -> bool:
    """True if the record has no LLM-derived premium (null, or all sub-fields null)."""
    if b is None:
        return True
    if isinstance(b, dict):
        return all(b.get(k) is None for k in ("monatlich_eur", "jaehrlich_eur"))
    return False


def known_stems() -> list[str]:
    return sorted(p.stem for p in TARIFFS.glob("*.json"))


def fail(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def merge_offer(key: str, offer: dict, offer_bytes: bytes) -> tuple[dict, list[str]]:
    """Merge one validated offer onto its pure tariff record. Returns (merged, paths).

    Does NOT write to disk — run_merge collects every merge and writes only if all
    succeed (all-or-nothing), so one bad offer never leaves a half-enriched set.
    """
    twin = TARIFFS / f"{key}.json"
    if not twin.exists():
        stems = known_stems()
        hint = (f"known tariffs: {', '.join(stems)}" if stems
                else "no out/tariffs/*.json at all — run scripts/extract.py first")
        fail(f"offer data/offers/{key}.json has no matching out/tariffs/{key}.json.\n"
             f"       Check the filename (it must equal the tariff stem); {hint}.")

    source = json.loads(twin.read_text(encoding="utf-8"))
    merged = copy.deepcopy(source)
    overridden: list[str] = []
    originals: dict[str, object] = {}

    # --- beitrag: replace the null premium wholesale; never clobber an LLM price ---
    bt = offer.get("beitrag")
    if bt is not None:
        # A present-but-empty beitrag (schema allows {}) used to be skipped silently by
        # `if bt:`, so a user who wrote the block got no premium and a success report.
        if bt.get("monatlich_eur") is None and bt.get("jaehrlich_eur") is None:
            fail(f"{key}: data/offers/{key}.json has an empty beitrag block (no "
                 f"monatlich_eur/jaehrlich_eur). Remove it or fill in a premium.")
        if not is_empty_beitrag(source.get("beitrag")):
            fail(f"{key}: out/tariffs record already has a non-null beitrag "
                 f"({source.get('beitrag')!r}). Overlay refuses to overwrite a price "
                 f"the extraction produced — the model may have invented it; investigate.")
        monatlich = bt.get("monatlich_eur")
        jaehrlich = bt.get("jaehrlich_eur")
        quelle = offer["quelle"]
        if monatlich is None and jaehrlich is not None:
            monatlich = round(jaehrlich / 12, 2)
            quelle = f"{quelle} (Monatsbeitrag aus Jahresbeitrag berechnet)"
        elif (monatlich is not None and jaehrlich is not None
              and abs(monatlich * 12 - jaehrlich) > 0.5):
            print(f"  note: {key}: beitrag monatlich {monatlich}*12={round(monatlich * 12, 2)} "
                  f"!= jaehrlich {jaehrlich} — copied verbatim; check the offer.",
                  file=sys.stderr)
        merged["beitrag"] = {"monatlich_eur": monatlich,
                             "jaehrlich_eur": jaehrlich, "quelle": quelle}
        overridden.append("beitrag")

    # --- module levels: only on included modules whose level is not already set ---
    # The pure twin is LLM-extracted and written without schema validation (extract.py),
    # so guard its shape here with an actionable error rather than crashing on a dict
    # index when a degraded/hand-edited record has modules:null or a non-dict module.
    mods = merged.setdefault("modules", {})
    if not isinstance(mods, dict):
        fail(f"{key}: out/tariffs/{key}.json has a malformed 'modules' "
             f"({type(mods).__name__}, expected an object). Re-run scripts/extract.py.")
    for m, pick in (offer.get("modules") or {}).items():
        if m not in mods:
            fail(f"{key}: offer sets a level for module '{m}', but the tariff record "
                 f"has no such module. Overlay never creates modules (the 'included' "
                 f"flag is the AVB-derived fact). Known modules: {', '.join(sorted(mods))}.")
        if not isinstance(mods[m], dict):
            fail(f"{key}: module '{m}' in the tariff record is not an object "
                 f"({type(mods[m]).__name__}); the twin is malformed — re-run extract.")
        if not mods[m].get("included"):
            fail(f"{key}: offer sets level '{pick['level']}' for module '{m}', but the "
                 f"tariff record marks it included=false (the AVB does not cover it). "
                 f"The Angebot disagrees with the AVB — resolve before overlaying.")
        if mods[m].get("level") is not None:
            fail(f"{key}: tariff record already has modules.{m}.level="
                 f"{mods[m]['level']!r} (extraction should leave the chosen Stufe null). "
                 f"A non-null extracted level is suspicious — investigate before overlaying.")
        mods[m]["level"] = pick["level"]
        overridden.append(f"modules.{m}.level")

    # --- chosen coverage values the AVB only lists as selectable options ---
    # Supersede the AVB-derived value (e.g. 'waehlbar 150/300' -> the chosen '150 EUR')
    # but keep the original in _overlay_original so the document-grounded fact is never
    # silently destroyed, and surface the supersede.
    cov = merged.setdefault("coverage", {})
    for k, v in (offer.get("coverage") or {}).items():
        old = cov.get(k)
        if old is not None and old != v:
            originals[f"coverage.{k}"] = old
            print(f"  note: {key}: coverage.{k} superseded — AVB-derived {old!r} -> "
                  f"offer {v!r} (original kept in _overlay_original).", file=sys.stderr)
        cov[k] = v
        overridden.append(f"coverage.{k}")

    # --- provenance + bookkeeping ---
    entry = {"doctype": offer.get("doctype", "angebot"),
             "content_sha256": hashlib.sha256(offer_bytes).hexdigest()}
    merged["sources"] = list(source.get("sources", [])) + [entry]
    merged["_overlay_fields"] = sorted(overridden)
    merged["_offer_sha256"] = entry["content_sha256"]
    if originals:
        merged["_overlay_original"] = originals

    self_check(key, source, merged, overridden, entry)
    return merged, overridden


def self_check(key: str, source: dict, merged: dict, overridden: list[str],
               entry: dict) -> None:
    """Containment guard: the merge may touch ONLY the fields it claims to.

    Not a faithfulness check (values are copied verbatim from a schema-validated
    file) — a blast-radius check that no LLM-derived fact was silently altered.
    """
    changed = diff_paths(comparable(source), comparable(merged))
    extra = sorted(c for c in changed if not covered_by(c, overridden))
    if extra:
        fail(f"{key}: overlay self-check failed — the merge changed fields it must "
             f"not touch: {', '.join(extra)}. Allowed: {', '.join(overridden) or '(none)'}. "
             f"Fix scripts/overlay.py; do not trust out/enriched/{key}.json.")

    # No module inclusion flag may flip (explicit, beyond the containment diff).
    for m, mod in (source.get("modules") or {}).items():
        # A module the offer never referenced is not guarded in merge_offer; tolerate a
        # malformed twin here too instead of crashing on .get before the schema gate.
        if not isinstance(mod, dict) or not isinstance(merged["modules"].get(m), dict):
            fail(f"{key}: modules.{m} is not an object in the tariff record — the twin "
                 f"is malformed; re-run scripts/extract.py.")
        if mod.get("included") != merged["modules"][m].get("included"):
            fail(f"{key}: overlay changed modules.{m}.included — forbidden.")

    if merged.get("sources") != list(source.get("sources", [])) + [entry]:
        fail(f"{key}: overlay self-check failed — sources is not the original plus "
             f"exactly one offer provenance entry.")

    # The merged record must still be a valid tariff record (sources required here,
    # since overlay always appends provenance). This is the only structural gate on
    # out/enriched/, because regression.py deliberately never reads it.
    schema = json.loads(TARIFF_SCHEMA.read_text(encoding="utf-8"))
    errs = sorted(Draft202012Validator(schema).iter_errors(
        {k: v for k, v in merged.items() if not k.startswith("_")}),
        key=lambda e: list(e.path))
    if errs:
        loc = "/".join(map(str, errs[0].path)) or "<root>"
        fail(f"{key}: merged record violates schema/tariff.schema.json at {loc}: "
             f"{errs[0].message}. Correct data/offers/{key}.json.")


def offer_files() -> list[Path]:
    """Offer files to process: data/offers/*.json minus the tracked template(s)."""
    return sorted(p for p in OFFERS.glob("*.json") if not p.name.startswith("_"))


def clear_enriched() -> None:
    """Remove stale enriched *twins* (the <key>.json overlay outputs) so a
    removed/renamed offer leaves no orphan record.

    Scoped to *.json on purpose: render.py writes non-JSON artifacts (e.g.
    marktanalyse.md, the rendered comparison) into out/enriched/ beside these
    records, and a re-merge must never delete those. out/enriched/ is fully derived
    and gitignored; regenerating the twins from scratch on every run keeps them
    idempotent (no cache) and prevents render from picking up a stale twin whose
    offer or pure record has since changed.
    """
    if ENRICHED.exists():
        for p in ENRICHED.glob("*.json"):
            if p.is_file():
                p.unlink()


def run_merge() -> int:
    assert_path_ignored(ENRICHED / "_probe.json", "out/enriched/")
    assert_path_ignored(OFFERS / "real_probe.json", "data/offers/ (real offers)")
    validator = Draft202012Validator(json.loads(OFFER_SCHEMA.read_text(encoding="utf-8")))
    offers = offer_files()

    if not offers:
        # A removed last offer should still leave no orphan twin behind.
        clear_enriched()
        print("  no data/offers/*.json (besides the template) — skipping enrichment.")
        return 0

    # All-or-nothing: validate and merge EVERY offer in memory FIRST; only once the
    # whole set is known good do we clear the stale twins and write. A single bad
    # offer must never empty out/enriched/ — clearing before validation once deleted
    # the rendered analysis (marktanalyse.md) when a later offer failed.
    results: list[tuple[str, dict, list[str]]] = []
    for path in offers:
        key = path.stem
        offer_bytes = path.read_bytes()
        offer = json.loads(offer_bytes.decode("utf-8"))
        errs = sorted(validator.iter_errors(offer), key=lambda e: list(e.path))
        if errs:
            loc = "/".join(map(str, errs[0].path)) or "<root>"
            fail(f"data/offers/{path.name} is invalid at {loc}: {errs[0].message}. "
                 f"See schema/offer.schema.json.")
        merged, overridden = merge_offer(key, offer, offer_bytes)
        results.append((key, merged, overridden))

    clear_enriched()
    ENRICHED.mkdir(parents=True, exist_ok=True)
    for key, merged, overridden in results:
        out_path = ENRICHED / f"{key}.json"
        out_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False),
                            encoding="utf-8")
        fields = ", ".join(overridden) or "provenance only"
        print(f"  overlay   {key}  ({fields})  -> {out_path.relative_to(ROOT)}")
    return 0


def run_check() -> int:
    """Re-validate existing enriched records against their pure twins, no merge.

    A standing gate: catches a hand-edited enriched file or a stale twin without
    pinning any euro value into a tracked file.
    """
    schema = json.loads(TARIFF_SCHEMA.read_text(encoding="utf-8"))
    files = sorted(ENRICHED.glob("*.json")) if ENRICHED.exists() else []
    if not files:
        print("  no out/enriched/*.json to check.")
        return 0
    failed = 0
    for path in files:
        key = path.stem
        twin = TARIFFS / f"{key}.json"
        merged = json.loads(path.read_text(encoding="utf-8"))
        problems: list[str] = []
        if not twin.exists():
            problems.append(f"no pure twin out/tariffs/{key}.json")
        else:
            source = json.loads(twin.read_text(encoding="utf-8"))
            for m, mod in (source.get("modules") or {}).items():
                if mod.get("included") != (merged.get("modules") or {}).get(m, {}).get("included"):
                    problems.append(f"modules.{m}.included differs from the pure twin")
            if len(merged.get("sources") or []) != len(source.get("sources") or []) + 1:
                problems.append("sources is not the twin's plus exactly one offer entry")
        bt = merged.get("beitrag")
        if isinstance(bt, dict) and bt.get("monatlich_eur") is not None and not bt.get("quelle"):
            problems.append("beitrag has a value but no quelle (provenance) label")
        errs = sorted(Draft202012Validator(schema).iter_errors(
            {k: v for k, v in merged.items() if not k.startswith("_")}),
            key=lambda e: list(e.path))
        if errs:
            problems.append(f"schema: {'/'.join(map(str, errs[0].path)) or '<root>'}: {errs[0].message}")
        if problems:
            failed += 1
            print(f"FAIL  {key}")
            for p in problems:
                print(f"        - {p}")
        else:
            print(f"PASS  {key}")
    if failed:
        print(f"\nOVERLAY CHECK: {failed}/{len(files)} enriched record(s) failed. "
              f"Re-run scripts/overlay.py to regenerate.", file=sys.stderr)
        return 1
    print(f"\nOK: {len(files)} enriched record(s) consistent with their pure twins.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Merge structured, non-LLM offer facts (premium, Stufe, "
                    "Selbstbeteiligung) onto the pure tariff records.")
    ap.add_argument("--check", action="store_true",
                    help="re-validate existing out/enriched/ against their pure "
                         "twins instead of merging")
    args = ap.parse_args()
    return run_check() if args.check else run_merge()


if __name__ == "__main__":
    raise SystemExit(main())
