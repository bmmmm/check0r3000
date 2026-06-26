"""Feature-history module: versioned tariff records in out/tariff-history/.

For each analyzed stem (out/tariffs/<stem>.json) a subdirectory holds one JSON
file per day on which the tariff's *comparable facts* changed. Extract.py calls
archive_version() after every successful write; the TUI uses state_as_of() and
diff_features() to show what changed between two snapshot dates.

Pure stdlib — no Textual, no uv deps. Can be run directly:
  python3 scripts/feature_history.py          # selftest + backfill dry-run
  python3 scripts/feature_history.py --backfill 2026-06-24   # seed existing records
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HISTORY_DIR = ROOT / "out" / "tariff-history"
TARIFFS_DIR = ROOT / "out" / "tariffs"

# Fields that define "comparable content" — pipeline metadata and PII premiums excluded
# so that re-extracting with a different model/prompt never generates phantom diffs.
_COMPARABLE = ("stand", "modules", "coverage", "leistungen",
               "ausschluesse", "besonderheiten", "beitrag")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _today() -> str:
    import datetime
    return datetime.date.today().isoformat()


def content_sha256(record: dict) -> str:
    """SHA-256 over the comparable fields only. Lists are sorted so reordering is ignored."""
    norm: dict = {}
    for k in _COMPARABLE:
        v = record.get(k)
        norm[k] = sorted(v) if isinstance(v, list) else v
    payload = json.dumps(norm, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _newest_version(stem_dir: Path) -> dict | None:
    """Return the most recent history entry for a stem, or None."""
    if not stem_dir.is_dir():
        return None
    candidates = sorted(
        p for p in stem_dir.glob("*.json") if _DATE_RE.match(p.stem)
    )
    if not candidates:
        return None
    try:
        return json.loads(candidates[-1].read_text(encoding="utf-8"))
    except Exception:
        return None


def archive_version(stem: str, record: dict, date: str | None = None) -> bool:
    """Archive `record` for `stem` if its comparable content changed since last version.

    Returns True when a new version was written, False when the content is
    unchanged (cache hit) or an error occurred.
    """
    date = date or _today()
    h = content_sha256(record)

    stem_dir = HISTORY_DIR / stem
    latest = _newest_version(stem_dir)

    if latest is not None and latest.get("_content_sha256") == h:
        return False  # no change in comparable facts

    entry: dict = {}
    for k in _COMPARABLE + ("insurer", "tariff"):
        if k in record:
            entry[k] = record[k]
    entry["stem"] = stem
    entry["_history_date"] = date
    entry["_content_sha256"] = h
    entry["_supersedes"] = latest["_history_date"] if latest else None

    stem_dir.mkdir(parents=True, exist_ok=True)
    dest = stem_dir / f"{date}.json"
    tmp = dest.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(entry, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, dest)
    except Exception as exc:
        print(f"feature_history: failed to write {dest}: {exc}", file=sys.stderr)
        tmp.unlink(missing_ok=True)
        return False
    return True


def state_as_of(stem: str, date: str) -> dict | None:
    """Return the feature state of `stem` as of `date` (largest history_date <= date).

    Returns None if the tariff has no history or was not yet analyzed by `date`.
    """
    stem_dir = HISTORY_DIR / stem
    if not stem_dir.is_dir():
        return None
    candidates = sorted(
        p for p in stem_dir.glob("*.json") if _DATE_RE.match(p.stem) and p.stem <= date
    )
    if not candidates:
        return None
    try:
        return json.loads(candidates[-1].read_text(encoding="utf-8"))
    except Exception:
        return None


def diff_features(old: dict, new: dict) -> dict:
    """Compute what changed between two history entries.

    Returns a dict with keys 'modules', 'coverage', 'leistungen', 'ausschluesse',
    'besonderheiten'. Each key is omitted when nothing changed there.
    Returns {} when the content hashes are equal.
    """
    if old.get("_content_sha256") and old["_content_sha256"] == new.get("_content_sha256"):
        return {}

    result: dict = {}

    # --- modules ---
    old_mods = old.get("modules") or {}
    new_mods = new.get("modules") or {}
    mod_changes = []
    all_mod_keys = sorted(set(old_mods) | set(new_mods))
    for key in all_mod_keys:
        om = old_mods.get(key) or {}
        nm = new_mods.get(key) or {}
        old_inc = om.get("included")
        new_inc = nm.get("included")
        old_lvl = om.get("level")
        new_lvl = nm.get("level")
        if old_inc != new_inc or old_lvl != new_lvl:
            mod_changes.append({
                "key": key,
                "old_included": old_inc,
                "new_included": new_inc,
                "old_level": old_lvl,
                "new_level": new_lvl,
            })
    if mod_changes:
        result["modules"] = mod_changes

    # --- coverage ---
    old_cov = old.get("coverage") or {}
    new_cov = new.get("coverage") or {}
    cov_changes = []
    all_cov_keys = sorted(set(old_cov) | set(new_cov))
    for key in all_cov_keys:
        ov = old_cov.get(key)
        nv = new_cov.get(key)
        if ov != nv:
            cov_changes.append({"field": key, "old": ov, "new": nv})
    if cov_changes:
        result["coverage"] = cov_changes

    # --- list fields (leistungen, ausschluesse, besonderheiten) ---
    for field in ("leistungen", "ausschluesse", "besonderheiten"):
        old_set = set(old.get(field) or [])
        new_set = set(new.get(field) or [])
        added = sorted(new_set - old_set)
        removed = sorted(old_set - new_set)
        if added or removed:
            result[field] = {"added": added, "removed": removed}

    return result


def backfill(date: str) -> int:
    """Seed history for every stem in out/tariffs/ that has no history yet.

    Uses `date` as the _history_date (should be the oldest market snapshot date).
    Returns the number of entries written.
    """
    written = 0
    for p in sorted(TARIFFS_DIR.glob("*.json")):
        stem = p.stem
        if _newest_version(HISTORY_DIR / stem) is not None:
            continue  # already has history
        try:
            record = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"  skip {stem}: {exc}", file=sys.stderr)
            continue
        if archive_version(stem, record, date=date):
            print(f"  seeded {stem}  ({date})")
            written += 1
    return written


def last_analysis_date(stem: str) -> str | None:
    """Date of the most-recent history entry (any re-analysis, changed or not)."""
    newest = _newest_version(HISTORY_DIR / stem)
    return newest.get("_history_date") if newest else None


def change_count(stem: str) -> int:
    """Number of detected feature changes — consecutive version pairs whose
    *comparable facts* actually differ. Delegates to full_changelog so it never
    disagrees with the changelog: this is < (archived versions − 1) whenever a
    metadata-only write happened (e.g. a `stand`-only re-extract bumps the
    content hash but diff_features reports no field change)."""
    return len(full_changelog(stem))


def last_change_date(stem: str) -> str | None:
    """Date of the most-recent detected feature change, or None if no real diff
    exists. Derived from full_changelog so a metadata-only version (e.g. a
    `stand`-only re-extract) is not reported as a change date."""
    changelog = full_changelog(stem)
    return changelog[-1][1] if changelog else None


def first_seen_date(stem: str) -> str | None:
    """Date of the first (baseline) history entry, or None."""
    stem_dir = HISTORY_DIR / stem
    if not stem_dir.is_dir():
        return None
    files = sorted(p for p in stem_dir.glob("*.json") if _DATE_RE.match(p.stem))
    return files[0].stem if files else None


def full_changelog(stem: str) -> list:
    """Return [(old_date, new_date, diff), ...] for all consecutive version pairs that changed."""
    stem_dir = HISTORY_DIR / stem
    if not stem_dir.is_dir():
        return []
    files = sorted(p for p in stem_dir.glob("*.json") if _DATE_RE.match(p.stem))
    if len(files) < 2:
        return []
    result = []
    for old_f, new_f in zip(files, files[1:]):
        try:
            old = json.loads(old_f.read_text(encoding="utf-8"))
            new = json.loads(new_f.read_text(encoding="utf-8"))
        except Exception:
            continue
        d = diff_features(old, new)
        if d:
            result.append((old_f.stem, new_f.stem, d))
    return result


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Feature history utility")
    ap.add_argument("--backfill", metavar="DATE",
                    help="Seed initial history for all analyzed tariffs (date: YYYY-MM-DD)")
    ap.add_argument("--selftest", action="store_true", default=False,
                    help="Run internal smoke tests")
    args = ap.parse_args()

    if args.selftest or (not args.backfill):
        # --- selftest ---
        import tempfile, shutil

        print("Running selftest …")
        tmp = Path(tempfile.mkdtemp())
        _orig_hist = HISTORY_DIR

        # Monkey-patch HISTORY_DIR to a temp dir
        import feature_history as _self
        _self.HISTORY_DIR = tmp

        rec_a = {
            "insurer": "TestCo", "tariff": "Basic", "stand": "01.2026",
            "modules": {"privat": {"included": True, "level": "Komfort", "note": None}},
            "coverage": {"wartezeit_monate": 3, "versicherungssumme": "unbegrenzt"},
            "leistungen": ["Beratung", "Mediation"],
            "ausschluesse": ["Baufinanzierung"],
            "besonderheiten": [],
            "beitrag": None,
        }
        rec_b = dict(rec_a)
        rec_b = {**rec_a,
                 "leistungen": ["Beratung", "Mediation", "Strafkaution"],
                 "modules": {"privat": {"included": True, "level": "Premium", "note": None}},
                 "coverage": {**rec_a["coverage"], "wartezeit_monate": 0},
                 }

        assert _self.archive_version("testco__basic", rec_a, "2026-01-01"), "first write failed"
        assert not _self.archive_version("testco__basic", rec_a, "2026-01-02"), "duplicate write"
        assert _self.archive_version("testco__basic", rec_b, "2026-03-01"), "update write failed"

        s = _self.state_as_of("testco__basic", "2026-02-01")
        assert s is not None and s["_history_date"] == "2026-01-01", f"state_as_of wrong: {s}"
        s2 = _self.state_as_of("testco__basic", "2026-03-01")
        assert s2 is not None and s2["_history_date"] == "2026-03-01", f"state_as_of 2 wrong: {s2}"

        d = _self.diff_features(s, s2)
        assert "modules" in d, f"module diff missing: {d}"
        assert d["modules"][0]["old_level"] == "Komfort"
        assert d["modules"][0]["new_level"] == "Premium"
        assert "leistungen" in d
        assert "Strafkaution" in d["leistungen"]["added"]
        assert "coverage" in d
        assert any(c["field"] == "wartezeit_monate" for c in d["coverage"])

        empty = _self.diff_features(s, s)
        assert empty == {}, f"self-diff not empty: {empty}"

        shutil.rmtree(tmp)
        _self.HISTORY_DIR = _orig_hist
        print("selftest passed ✓")

    if args.backfill:
        if not _DATE_RE.match(args.backfill):
            print(f"Invalid date format: {args.backfill!r} (expected YYYY-MM-DD)", file=sys.stderr)
            sys.exit(1)
        n = backfill(args.backfill)
        print(f"Backfill complete: {n} entries written.")
