"""check0r3000 — single source of truth for the canonical Baustein keys/labels.

Stdlib-only leaf (no textual/rich/third-party imports) so every stage — the Magic
scorer (magic.py), the eval harness (eval.py), the offline renderer (render.py) and
the TUI (tui_app.py) — derives the module keys and their German labels from one place
instead of re-declaring them. The re-declarations had drifted: the renderer showed
'Wohnen/Immobilien' / 'Verwaltung' while the TUI showed 'Wohnen' / 'Verwaltungsrecht'.

Keys come from the vertical's schema (schema/<v>/tariff.schema.json,
properties.modules.properties) so the schema stays the authority for which modules
exist and in which order; labels come from the vertical's config data
(config/verticals/<v>/vertical.json, `module_labels`).

Two access styles:
  * module_keys() / module_labels() — resolve the ACTIVE vertical at call time,
    cached per vertical; the TUI uses these so an in-process vertical switch
    (reset_cache() + reload) shows the right Bausteine.
  * MODULE_KEYS / MODULE_LABELS — snapshot of the active vertical at import,
    for process-scoped CLI scripts (eval.py, render.py) whose vertical is fixed.
"""
from __future__ import annotations

import json

import _vertical

_keys_cache: dict[str, tuple[str, ...]] = {}
_labels_cache: dict[str, dict[str, str]] = {}


def reset_cache() -> None:
    """Drop the per-vertical caches (the TUI calls this on a vertical switch)."""
    _keys_cache.clear()
    _labels_cache.clear()


def _load_module_keys(vertical: str | None = None) -> tuple[str, ...]:
    schema_path = _vertical.tariff_schema_path(vertical)
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"tariff schema not found at {schema_path} — the module keys are derived "
            "from it; restore it (or scaffold the vertical first)."
        ) from exc
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(
            f"tariff schema at {schema_path} is unreadable/malformed ({exc}); fix the "
            "JSON so the module keys can be derived."
        ) from exc
    try:
        props = schema["properties"]["modules"]["properties"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError(
            f"tariff schema at {schema_path} has no properties.modules.properties "
            f"({exc}); cannot derive the module keys — check the schema shape."
        ) from exc
    keys = tuple(props)  # dict preserves insertion (== schema) order
    if not keys:
        raise RuntimeError(
            f"tariff schema at {schema_path} declares no modules under "
            "properties.modules.properties; expected the vertical's Bausteine."
        )
    return keys


def module_keys(vertical: str | None = None) -> tuple[str, ...]:
    v = vertical or _vertical.active()
    if v not in _keys_cache:
        _keys_cache[v] = _load_module_keys(v)
    return _keys_cache[v]


def module_labels(vertical: str | None = None) -> dict[str, str]:
    """Canonical presentation labels for the vertical's module keys. Labels are
    per-vertical DATA; an un-curated vertical (no labels yet) falls back to the
    raw schema keys so nothing crashes while scaffolding."""
    v = vertical or _vertical.active()
    if v not in _labels_cache:
        labels = _vertical.vertical_config(v).get("module_labels")
        if not isinstance(labels, dict):
            labels = {}
        _labels_cache[v] = {k: str(labels.get(k) or k) for k in module_keys(v)}
    return _labels_cache[v]


MODULE_KEYS: tuple[str, ...] = module_keys()
MODULE_LABELS: dict[str, str] = module_labels()
