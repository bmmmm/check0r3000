"""check0r3000 — shared JSON IO helpers.

Stdlib-only leaf (no project imports), mirroring the style of _modules.py / _filter.py.
Named _jsonio, not _io: `_io` collides with CPython's built-in `_io` module (compiled
in, resolved before sys.path), which would silently shadow this file on import.

About eight writers in this repo (feature_history.py, extract.py, tui_app.py,
snapshot.py, ...) each hand-roll the same tmp-file + os.replace dance, and several
loaders each catch a different exception tuple for "file missing or unreadable". This
centralizes both so a new writer/loader can't drift from the established convention.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def atomic_write_json(path: Path, obj, **dumps_kwargs) -> None:
    """Write `obj` as JSON to `path` via a tmp twin in the same dir + os.replace.

    A crash (Ctrl-C, disk full, kill) mid-write must never leave a truncated file
    where a tracked or hand-curated JSON used to be. Defaults (indent=2,
    ensure_ascii=False, trailing newline) match what the existing hand-rolled writers
    in this repo already produce — override via dumps_kwargs if a caller needs
    something else.
    """
    dumps_kwargs.setdefault("indent", 2)
    dumps_kwargs.setdefault("ensure_ascii", False)
    text = json.dumps(obj, **dumps_kwargs) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def load_json_or(path: Path, default):
    """Read+parse JSON at `path`, returning `default` on any read/parse failure.

    Unifies the drifted exception tuples across loaders (some catch only OSError,
    some only FileNotFoundError) into one (OSError, json.JSONDecodeError) — covers a
    missing file, a permission error and malformed JSON alike.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
