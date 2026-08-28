"""check0r3000 — single source of truth for loading data/sources/check24-documents.json.

Stdlib-only leaf (mirrors _modules.py), used by fetch_docs.py (reads only) and
harvest_docs.py (reads, merges, writes back). Both used to carry their own
load_manifest() with divergent guards and return shapes — one bailed out on a missing
file, the other synthesized a fresh skeleton; one validated every tariff/doc shape, the
other only checked the top-level type. Consolidated here so every caller gets the same
validation and the same shape.
"""
from __future__ import annotations

import json
import sys

import _vertical

ROOT = _vertical.ROOT
MANIFEST = _vertical.manifest_path()

DEFAULT_MANIFEST: dict = {
    "quelle": "check24 rsv vergleichsergebnis (all insurers) — harvest_docs.py",
    "host": "https://rechtsschutz.check24.de",
    "note": "Source-PDF URLs only (PDFs are third-party/copyright -> data/raw is "
            "gitignored). fetch_docs.py downloads on demand.",
    "tariffs": [],
}


def load_manifest(create_if_missing: bool = False) -> dict:
    """Load and validate data/sources/check24-documents.json.

    Returns the full manifest dict: {quelle, host, tariffs: [...]}, each tariff an
    object with an optional list `docs` of {doctype, url} objects (url a string). The
    manifest is hand-reshaped from the browser harvest with no schema gate, so every
    shape is checked here once — a typo fails with an actionable message instead of a
    mid-batch traceback deep in urlsplit()/download().

    create_if_missing: harvest_docs.py's first run has no manifest yet and builds one
    from scratch; fetch_docs.py only ever reads an existing one, so a missing file there
    is a hard error naming the harvest step that would create it.
    """
    if not MANIFEST.exists():
        if create_if_missing:
            return json.loads(json.dumps(DEFAULT_MANIFEST))  # defensive copy
        sys.exit(f"No manifest at {MANIFEST.relative_to(ROOT)} — run the browser harvest "
                 f"first (scripts/harvest_docs.py, or scripts/check24_scrape.js -> "
                 f"check24Docs).")
    try:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        sys.exit(f"{MANIFEST.relative_to(ROOT)} is not valid JSON ({exc}) — fix or "
                 f"restore it before continuing.")
    if isinstance(data, list):
        sys.exit(f"{MANIFEST.relative_to(ROOT)} is a bare list (raw check24Docs() output, "
                 f"grouped by hash). Wrap it as {{\"tariffs\": [{{stem, insurer, tariff, "
                 f"docs:[{{doctype, url}}]}}]}} — see data/offers/README.md.")
    if not isinstance(data, dict):
        sys.exit(f"{MANIFEST.relative_to(ROOT)} is not an object — expected "
                 "{quelle, host, tariffs:[...]}.")
    data.setdefault("tariffs", [])
    tariffs = data["tariffs"]
    if not isinstance(tariffs, list):
        sys.exit(f"{MANIFEST.relative_to(ROOT)}: 'tariffs' must be a list "
                 f"({type(tariffs).__name__} found).")
    for t in tariffs:
        if not isinstance(t, dict):
            sys.exit(f"Malformed manifest: a tariff entry is not an object "
                     f"({type(t).__name__}).")
        docs = t.get("docs")
        if docs is not None and not isinstance(docs, list):
            sys.exit(f"Malformed manifest entry {t.get('stem')!r}: 'docs' must be a list.")
        for doc in (docs or []):
            if not isinstance(doc, dict) or not isinstance(doc.get("url", ""), str):
                sys.exit(f"Malformed manifest entry {t.get('stem')!r}: each doc must be "
                         f"{{doctype, url}} with a string url — fix "
                         f"data/sources/check24-documents.json.")
    return data
