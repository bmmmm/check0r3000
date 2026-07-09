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
from pathlib import Path

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema" / "tariff.schema.json"


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

# Canonical German labels (presentation only). Keys mirror MODULE_KEYS; the fuller
# 'Wohnen/Immobilien' / 'Verwaltungsrecht' / 'Internet/Web' forms are authoritative
# (render.py and the TUI had drifted to shorter variants). Every label fits the fixed
# 30-char Vergleich label column and the 22-char detail band, so no short override is
# needed.
MODULE_LABELS: dict[str, str] = {
    "privat": "Privat",
    "beruf": "Beruf",
    "verkehr": "Verkehr",
    "wohnen_immobilien": "Wohnen/Immobilien",
    "internet_web": "Internet/Web",
    "steuer": "Steuer",
    "sozialgericht": "Sozialgericht",
    "verwaltungsrecht": "Verwaltungsrecht",
}
