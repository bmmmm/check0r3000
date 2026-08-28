"""check0r3000 — single source of truth for the eight canonical Baustein keys/labels.

Stdlib-only leaf (no textual/rich/third-party imports) so every stage — the Magic
scorer (magic.py), the eval harness (eval.py), the offline renderer (render.py) and
the TUI (tui_app.py) — derives the module keys and their German labels from one place
instead of re-declaring them. The re-declarations had drifted: the renderer showed
'Wohnen/Immobilien' / 'Verwaltung' while the TUI showed 'Wohnen' / 'Verwaltungsrecht'.

MODULE_KEYS is read from schema/tariff.schema.json (properties.modules.properties) so
the schema stays the authority for which modules exist and in which order; MODULE_LABELS
is the canonical presentation dict.
"""
from __future__ import annotations

import json

import _vertical

_SCHEMA_PATH = _vertical.tariff_schema_path()


def _load_module_keys() -> tuple[str, ...]:
    try:
        schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"tariff schema not found at {_SCHEMA_PATH} — the module keys are derived "
            "from it; restore schema/tariff.schema.json."
        ) from exc
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(
            f"tariff schema at {_SCHEMA_PATH} is unreadable/malformed ({exc}); fix the "
            "JSON so the module keys can be derived."
        ) from exc
    try:
        props = schema["properties"]["modules"]["properties"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError(
            f"tariff schema at {_SCHEMA_PATH} has no properties.modules.properties "
            f"({exc}); cannot derive the module keys — check the schema shape."
        ) from exc
    keys = tuple(props)  # dict preserves insertion (== schema) order
    if not keys:
        raise RuntimeError(
            f"tariff schema at {_SCHEMA_PATH} declares no modules under "
            "properties.modules.properties; expected the eight Bausteine."
        )
    return keys


MODULE_KEYS: tuple[str, ...] = _load_module_keys()


def _load_module_labels(keys: tuple[str, ...]) -> dict[str, str]:
    """Canonical presentation labels, from the vertical's config data
    (config/verticals/<v>/vertical.json, key `module_labels`). Labels are
    per-vertical DATA, not code; an un-curated vertical (no labels yet) falls
    back to the raw schema keys so nothing crashes while scaffolding."""
    labels = _vertical.vertical_config().get("module_labels")
    if not isinstance(labels, dict):
        labels = {}
    return {k: str(labels.get(k) or k) for k in keys}


MODULE_LABELS: dict[str, str] = _load_module_labels(MODULE_KEYS)
