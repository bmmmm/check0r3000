"""check0r3000 — single path resolver for the vertical namespace.

Stdlib-only leaf (mirrors _modules.py / _manifest.py / _jsonio.py): every script that
used to hold its own `ROOT / "data" / ...` constants routes its paths through this
module instead, so the active insurance vertical (Rechtsschutz, Hausrat, ...) is one
switch instead of ~24 scattered constants.

The active vertical comes from the CHECK0R_VERTICAL environment variable (default:
the registry's `default`, currently `rechtsschutz`). `active()` re-reads the
environment on every call, so the TUI can switch verticals at runtime via
`set_active()` and child processes (pipeline.sh, extract.py, ...) inherit the choice
through their environment.

config/verticals.json is the registry — the data source of the TUI's vertical
selector: per vertical `{label, host, funnel_path, status}` with status one of
production | experimental | disabled.

Per-vertical *configuration* (module labels, filter anchors, extraction instruction,
query-param map, ...) lives in config/verticals/<v>/vertical.json and is served by
`vertical_config()`.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = ROOT / "config" / "verticals.json"
TMP = ROOT / "tmp"  # run artifacts (logs, reports, eval) are global, not per-vertical

# Layout flag: False = the vertical namespace (data/<v>/raw, out/<v>/tariffs,
# schema/<v>/tariff.schema.json, config/verticals/<v>/...). True was the
# historical single-vertical tree; the flip happened in the same commit as the
# git-mv migration, keeping consumer rewiring and the physical move two
# separately verifiable steps.
_FLAT = False

_registry_cache: dict | None = None
_config_cache: dict[str, dict] = {}


def reload() -> None:
    """Drop the registry/vertical-config caches (used by the TUI after edits)."""
    global _registry_cache
    _registry_cache = None
    _config_cache.clear()


def registry() -> dict:
    """The full config/verticals.json. A missing/broken registry is a hard error —
    every path decision depends on it."""
    global _registry_cache
    if _registry_cache is None:
        try:
            _registry_cache = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError:
            sys.exit(f"vertical registry missing at {REGISTRY_PATH} — restore "
                     "config/verticals.json (it is tracked).")
        except (OSError, json.JSONDecodeError) as exc:
            sys.exit(f"vertical registry at {REGISTRY_PATH} is unreadable/malformed "
                     f"({exc}) — fix the JSON.")
        if not isinstance(_registry_cache.get("verticals"), dict):
            sys.exit(f"{REGISTRY_PATH}: expected an object under 'verticals'.")
    return _registry_cache


def default_vertical() -> str:
    return registry().get("default") or "rechtsschutz"


def active() -> str:
    """The active vertical: CHECK0R_VERTICAL if set, else the registry default.
    An unknown name is a hard error — silently resolving paths into a nonexistent
    namespace would read as 'no data' everywhere instead of as a typo."""
    v = os.environ.get("CHECK0R_VERTICAL", "").strip() or default_vertical()
    if v not in registry()["verticals"]:
        known = ", ".join(sorted(registry()["verticals"]))
        sys.exit(f"unknown vertical {v!r} (CHECK0R_VERTICAL) — known: {known}")
    return v


def set_active(vertical: str) -> None:
    """Switch the active vertical for this process AND every child it spawns."""
    if vertical not in registry()["verticals"]:
        known = ", ".join(sorted(registry()["verticals"]))
        raise ValueError(f"unknown vertical {vertical!r} — known: {known}")
    os.environ["CHECK0R_VERTICAL"] = vertical


def entry(vertical: str | None = None) -> dict:
    """The registry entry {label, host, funnel_path, status} of a vertical."""
    v = vertical or active()
    e = registry()["verticals"].get(v)
    if not isinstance(e, dict):
        sys.exit(f"vertical {v!r} has no registry entry in {REGISTRY_PATH}.")
    return e


def selectable(include_disabled: bool = False) -> list[str]:
    """Vertical names for the TUI selector: registry order, disabled filtered out."""
    return [v for v, e in registry()["verticals"].items()
            if include_disabled or (e.get("status") != "disabled")]


def run_per_vertical(argv: list[str]) -> int:
    """Run `argv` once per non-disabled registry vertical, each in a subprocess
    with CHECK0R_VERTICAL set; the worst return code wins. Shared by every
    script's --all-verticals flag (regression, selftests) so CI sweeps the whole
    registry with one line per gate."""
    import subprocess
    rc = 0
    for v in selectable():
        print(f"\n===== vertical: {v} =====", flush=True)
        res = subprocess.run(argv, env={**os.environ, "CHECK0R_VERTICAL": v})
        rc = max(rc, res.returncode)
    return rc


def vertical_config(vertical: str | None = None) -> dict:
    """config/verticals/<v>/vertical.json — per-vertical configuration data
    (module labels, filter anchors, instruction text, query-param map, ...).
    Returns {} when the file does not exist (the flat layout has none)."""
    v = vertical or active()
    if v not in _config_cache:
        path = vertical_json_path(v)
        try:
            _config_cache[v] = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            _config_cache[v] = {}
        except (OSError, json.JSONDecodeError) as exc:
            sys.exit(f"vertical config at {path} is unreadable/malformed ({exc}).")
    return _config_cache[v]


def _v(vertical: str | None) -> str:
    return vertical or active()


def _base(kind: str, vertical: str | None) -> Path:
    """Namespace root for one of data|out|schema|benchmarks|config."""
    if _FLAT:
        return ROOT / kind
    if kind == "config":
        return ROOT / "config" / "verticals" / _v(vertical)
    return ROOT / kind / _v(vertical)


# --- data/ ---------------------------------------------------------------------
def data_dir(vertical: str | None = None) -> Path:
    return _base("data", vertical)


def inbox_dir(vertical: str | None = None) -> Path:
    return data_dir(vertical) / "inbox"


def raw_dir(vertical: str | None = None) -> Path:
    return data_dir(vertical) / "raw"


def extracted_dir(vertical: str | None = None) -> Path:
    return data_dir(vertical) / "extracted"


def snapshots_dir(vertical: str | None = None) -> Path:
    return data_dir(vertical) / "snapshots"


def sources_dir(vertical: str | None = None) -> Path:
    return data_dir(vertical) / "sources"


def offers_dir(vertical: str | None = None) -> Path:
    return data_dir(vertical) / "offers"


def manifest_path(vertical: str | None = None) -> Path:
    return sources_dir(vertical) / "check24-documents.json"


def external_ratings_path(vertical: str | None = None) -> Path:
    return sources_dir(vertical) / "external-ratings.json"


# --- out/ ----------------------------------------------------------------------
def out_dir(vertical: str | None = None) -> Path:
    return _base("out", vertical)


def tariffs_dir(vertical: str | None = None) -> Path:
    return out_dir(vertical) / "tariffs"


def enriched_dir(vertical: str | None = None) -> Path:
    return out_dir(vertical) / "enriched"


def history_dir(vertical: str | None = None) -> Path:
    return out_dir(vertical) / "tariff-history"


def screenshots_dir(vertical: str | None = None) -> Path:
    return out_dir(vertical) / "screenshots"


def vergleich_path(vertical: str | None = None) -> Path:
    return out_dir(vertical) / "vergleich.md"


def index_html_path(vertical: str | None = None) -> Path:
    return out_dir(vertical) / "index.html"


# --- schema/ -------------------------------------------------------------------
def schema_dir(vertical: str | None = None) -> Path:
    return _base("schema", vertical)


def tariff_schema_path(vertical: str | None = None) -> Path:
    return schema_dir(vertical) / "tariff.schema.json"


def offer_schema_path(vertical: str | None = None) -> Path:
    return schema_dir(vertical) / "offer.schema.json"


# --- benchmarks/ ---------------------------------------------------------------
def benchmarks_dir(vertical: str | None = None) -> Path:
    return _base("benchmarks", vertical)


def golden_path(vertical: str | None = None) -> Path:
    return benchmarks_dir(vertical) / "golden.json"


def results_json_path(vertical: str | None = None) -> Path:
    return benchmarks_dir(vertical) / "results.json"


# --- config/ -------------------------------------------------------------------
def config_dir(vertical: str | None = None) -> Path:
    return _base("config", vertical)


def vertical_json_path(vertical: str | None = None) -> Path:
    return config_dir(vertical) / "vertical.json"


def profile_path(vertical: str | None = None) -> Path:
    return config_dir(vertical) / "check24-profile.json"


def profile_example_path(vertical: str | None = None) -> Path:
    return config_dir(vertical) / "check24-profile.example.json"


def providers_path(vertical: str | None = None) -> Path:
    return config_dir(vertical) / "check24-providers.json"


def taxonomy_path(vertical: str | None = None) -> Path:
    return config_dir(vertical) / "coverage_taxonomy.json"


def magic_weights_path(vertical: str | None = None) -> Path:
    return config_dir(vertical) / "magic-weights.json"


def needs_weights_path(vertical: str | None = None) -> Path:
    return config_dir(vertical) / "needs-weights.json"


def favorites_path(vertical: str | None = None) -> Path:
    return config_dir(vertical) / "favorites.json"


def favorite_notes_path(vertical: str | None = None) -> Path:
    return config_dir(vertical) / "favorite-notes.json"


def _selftest() -> int:
    """Textual-free smoke test: registry loads, default resolves, every path helper
    yields a ROOT-relative path in the expected namespace, unknown verticals fail."""
    failures: list[str] = []

    def check(cond: bool, what: str) -> None:
        print(("  ok   " if cond else "  FAIL ") + what)
        if not cond:
            failures.append(what)

    reg = registry()
    check(isinstance(reg.get("verticals"), dict) and reg["verticals"],
          "registry has verticals")
    check(default_vertical() in reg["verticals"], "default vertical is registered")

    prev = os.environ.pop("CHECK0R_VERTICAL", None)
    try:
        v = active()
        check(v == default_vertical(), f"active() falls back to default ({v})")
        e = entry(v)
        check(bool(e.get("label")) and bool(e.get("host")), "entry has label+host")
        check(e.get("status") in ("production", "experimental", "disabled"),
              "entry status is a known value")
        check(v in selectable(), "default vertical is selectable")

        expected = {
            data_dir(): "data", inbox_dir(): "data/inbox", raw_dir(): "data/raw",
            extracted_dir(): "data/extracted", snapshots_dir(): "data/snapshots",
            sources_dir(): "data/sources", offers_dir(): "data/offers",
            manifest_path(): "data/sources/check24-documents.json",
            external_ratings_path(): "data/sources/external-ratings.json",
            out_dir(): "out", tariffs_dir(): "out/tariffs",
            enriched_dir(): "out/enriched", history_dir(): "out/tariff-history",
            screenshots_dir(): "out/screenshots",
            vergleich_path(): "out/vergleich.md", index_html_path(): "out/index.html",
            tariff_schema_path(): "schema/tariff.schema.json",
            offer_schema_path(): "schema/offer.schema.json",
            benchmarks_dir(): "benchmarks", golden_path(): "benchmarks/golden.json",
            results_json_path(): "benchmarks/results.json",
            config_dir(): "config",
            profile_path(): "config/check24-profile.json",
            taxonomy_path(): "config/coverage_taxonomy.json",
            favorites_path(): "config/favorites.json",
        } if _FLAT else {
            data_dir(): f"data/{v}", raw_dir(): f"data/{v}/raw",
            extracted_dir(): f"data/{v}/extracted",
            snapshots_dir(): f"data/{v}/snapshots",
            manifest_path(): f"data/{v}/sources/check24-documents.json",
            external_ratings_path(): f"data/{v}/sources/external-ratings.json",
            tariffs_dir(): f"out/{v}/tariffs", enriched_dir(): f"out/{v}/enriched",
            history_dir(): f"out/{v}/tariff-history",
            vergleich_path(): f"out/{v}/vergleich.md",
            tariff_schema_path(): f"schema/{v}/tariff.schema.json",
            offer_schema_path(): f"schema/{v}/offer.schema.json",
            golden_path(): f"benchmarks/{v}/golden.json",
            config_dir(): f"config/verticals/{v}",
            profile_path(): f"config/verticals/{v}/check24-profile.json",
            taxonomy_path(): f"config/verticals/{v}/coverage_taxonomy.json",
            favorites_path(): f"config/verticals/{v}/favorites.json",
            vertical_json_path(): f"config/verticals/{v}/vertical.json",
        }
        for got, rel in expected.items():
            check(got == ROOT / rel, f"{rel} resolves ({got.relative_to(ROOT)})")

        cfg = vertical_config(v)
        check(isinstance(cfg, dict), "vertical_config returns a dict")

        os.environ["CHECK0R_VERTICAL"] = "no-such-vertical"
        try:
            active()
        except SystemExit:
            check(True, "unknown CHECK0R_VERTICAL exits loudly")
        else:
            check(False, "unknown CHECK0R_VERTICAL exits loudly")
        try:
            set_active("no-such-vertical")
        except ValueError:
            check(True, "set_active rejects unknown verticals")
        else:
            check(False, "set_active rejects unknown verticals")
    finally:
        if prev is None:
            os.environ.pop("CHECK0R_VERTICAL", None)
        else:
            os.environ["CHECK0R_VERTICAL"] = prev

    print(("_vertical selftest PASSED" if not failures
           else f"_vertical selftest FAILED ({len(failures)})"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
