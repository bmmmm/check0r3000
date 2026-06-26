"""check0r3000 — data-loading layer.

Textual-free leaf module: snapshot/detail dataclasses and every load_* helper,
the manifest stem resolver, and the non-interactive selftest. Imported by tui.py
as a sibling; also importable (and runnable as `python3 tui_data.py`) under a
plain interpreter with no Textual installed, for a zero-dependency data smoke
test."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# .resolve() so the repo root is found even when the launcher is reached through
# a symlink (e.g. ~/.local/bin/check0r3000 -> scripts/tui.py); tui_data.py itself
# is always imported by its real path, so __file__ here is scripts/tui_data.py.
REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Data loading helpers
# ---------------------------------------------------------------------------


def _find_latest_snapshot(snapshot_dir: Path) -> Path | None:
    """Return the most-recent snapshot JSON by filename date, or None.

    snapshot.py names files YYYY-MM-DD.json, so a lexicographic sort is also
    chronological — but only over date-named files. Restrict the glob to that
    pattern so a stray non-date *.json (a backup, an export) can't sort last and
    masquerade as the latest snapshot."""
    if not snapshot_dir.is_dir():
        return None
    import re

    candidates = sorted(p for p in snapshot_dir.glob("*.json")
                        if re.match(r"\d{4}-\d{2}-\d{2}", p.stem))
    return candidates[-1] if candidates else None


@dataclass
class SnapshotRow:
    """One row from a market snapshot."""

    position: int
    insurer: str
    product: str
    tarifnote: str
    monatlich_eur: float | None
    selbstbeteiligung: str
    key: str

    # CHECK24 customer rating (separate from the expert Tarifnote), if scraped.
    bewertung: float | None = None
    bewertung_anzahl: int | None = None
    # Per-Baustein wait times from the listing (Privat/Beruf/Wohnen/Verkehr → "N Monate").
    wartezeit_per_modul: dict | None = None

    # enriched at load time
    stem: str | None = None  # canonical tariff id (from the URL manifest)
    has_urls: bool = False   # source-document URLs harvested (manifest entry exists)
    has_pdf: bool = False    # source PDFs downloaded locally (data/raw/<stem>/)
    has_detail: bool = False  # analyzed record present (out/<…>/<stem>.json)
    has_offer: bool = False


@dataclass
class Snapshot:
    date: str
    profile: str
    source: str
    count: int
    rows: list[SnapshotRow] = field(default_factory=list)


@dataclass
class DetailRecord:
    """Loaded out/tariffs or out/enriched record."""

    insurer: str
    tariff: str
    stand: str | None
    modules: dict[str, Any]
    coverage: dict[str, Any]
    leistungen: list[str]
    ausschluesse: list[str]
    besonderheiten: list[str]
    beitrag: dict[str, Any] | None
    is_enriched: bool = False


def _slug(insurer: str, product: str) -> str:
    """Derive a loose slug to match detail filenames."""
    import re

    def slugify(s: str) -> str:
        s = s.lower()
        s = re.sub(r"[äöüß]",
                   lambda m: {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}[m.group()], s)
        s = re.sub(r"[^a-z0-9]+", "-", s)
        s = s.strip("-")
        return s

    return f"{slugify(insurer)}__{slugify(product)}"


def _as_dict(v: Any) -> dict:
    return v if isinstance(v, dict) else {}


def _as_list(v: Any) -> list:
    return v if isinstance(v, list) else []


def _record_from_data(
    data: dict, is_enriched: bool, insurer: str, product: str
) -> DetailRecord:
    return DetailRecord(
        insurer=data.get("insurer", insurer),
        tariff=data.get("tariff", product),
        stand=data.get("stand"),
        # isinstance-guard, not `or {}`: a model may emit an explicit JSON null OR a
        # wrong type (a list, a string) for these. extract.py only warns on empty
        # modules/coverage and writes the record anyway, and any non-dict/non-list
        # here crashes the render sites (rec.modules.get(...), for x in leistungen).
        # Guard at the *element* level too: a non-dict per-module value (modules.privat
        # = "Premium") and non-string benefit items crash the same render sites that the
        # container guard alone leaves exposed.
        modules={k: v for k, v in _as_dict(data.get("modules")).items()
                 if isinstance(v, dict)},
        coverage=_as_dict(data.get("coverage")),
        leistungen=[str(x) for x in _as_list(data.get("leistungen"))],
        ausschluesse=[str(x) for x in _as_list(data.get("ausschluesse"))],
        besonderheiten=[str(x) for x in _as_list(data.get("besonderheiten"))],
        beitrag=data.get("beitrag") if isinstance(data.get("beitrag"), dict) else None,
        is_enriched=is_enriched,
    )


def _detail_path_for_stem(stem: str) -> tuple[Path, bool] | None:
    """Locate the analyzed record for a canonical stem (enriched preferred)."""
    for sub, is_enriched in (("enriched", True), ("tariffs", False)):
        path = REPO_ROOT / "out" / sub / f"{stem}.json"
        if path.is_file():
            return path, is_enriched
    return None


def _load_detail_by_stem(stem: str) -> DetailRecord | None:
    hit = _detail_path_for_stem(stem)
    if hit is None:
        return None
    path, is_enriched = hit
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return _record_from_data(data, is_enriched, data.get("insurer", ""), data.get("tariff", ""))


def _load_detail(insurer: str, product: str) -> DetailRecord | None:
    """Load the analyzed record for a tariff.

    The canonical key is the manifest `stem` (the same join the docs/[g] path uses);
    this resolves the long-standing mismatch where the pipeline named files from the
    PDF filename while the TUI looked them up from the snapshot's DOM strings. Falls
    back to the slug only for records with no manifest entry (legacy/manual)."""
    stem = resolve_stem(insurer, product)
    if stem:
        rec = _load_detail_by_stem(stem)
        if rec is not None:
            return rec

    slug = _slug(insurer, product)
    for sub, is_enriched in (("enriched", True), ("tariffs", False)):
        path = REPO_ROOT / "out" / sub / f"{slug}.json"
        if path.is_file():
            try:
                return _record_from_data(json.loads(path.read_text()), is_enriched, insurer, product)
            except (json.JSONDecodeError, OSError):
                pass
    return None


def load_all_details() -> list[tuple[str, DetailRecord]]:
    """Every analyzed tariff record on disk, as (stem, record).

    Globs out/enriched then out/tariffs, deduping by stem with enriched preferred
    (mirroring _detail_path_for_stem), skipping bookkeeping files (_-prefixed). This
    is exactly the set the Vergleich view compares — adding favorites and analyzing
    them via [g]/[G] grows it with no schema change.
    """
    out: dict[str, DetailRecord] = {}
    for sub, is_enriched in (("enriched", True), ("tariffs", False)):
        directory = REPO_ROOT / "out" / sub
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            stem = path.stem
            if stem.startswith("_") or stem in out:
                continue
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            out[stem] = _record_from_data(
                data, is_enriched, data.get("insurer", ""), data.get("tariff", "")
            )
    return list(out.items())


def _tracked_keys() -> set[str]:
    """Return set of slugs present in out/tariffs/, out/enriched/, or data/offers/."""
    keys: set[str] = set()
    for directory in [
        REPO_ROOT / "out" / "tariffs",
        REPO_ROOT / "out" / "enriched",
        REPO_ROOT / "data" / "offers",
    ]:
        if directory.is_dir():
            for p in directory.glob("*.json"):
                if not p.name.startswith("_"):
                    keys.add(p.stem)
    return keys


def load_snapshot(path: Path) -> Snapshot | None:
    """Load a snapshot JSON. Returns None if file missing or malformed."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None

    tracked = _tracked_keys()
    rows: list[SnapshotRow] = []
    for t in data.get("tariffs", []):
        if not isinstance(t, dict):
            continue
        insurer = t.get("insurer", "")
        product = t.get("product", "")
        slug = _slug(insurer, product)
        stem = resolve_stem(insurer, product)
        has_detail = bool(stem and _detail_path_for_stem(stem)) or (slug in tracked)
        row = SnapshotRow(
            position=t.get("position", 0),
            insurer=insurer,
            product=product,
            tarifnote=t.get("tarifnote", ""),
            monatlich_eur=t.get("monatlich_eur"),
            selbstbeteiligung=t.get("selbstbeteiligung", ""),
            key=t.get("key", ""),
            bewertung=t.get("bewertung"),
            bewertung_anzahl=t.get("bewertung_anzahl"),
            wartezeit_per_modul=(
                t.get("wartezeit_per_modul")
                if isinstance(t.get("wartezeit_per_modul"), dict)
                else None
            ),
            stem=stem,
            has_urls=stem is not None,
            has_pdf=bool(stem and _raw_dir_for_stem(stem).is_dir()),
            has_detail=has_detail,
            # Offers are named by the canonical stem (e.g. arag__premium-2026.json),
            # not the loose DOM slug (arag__premium) — check the stem first so the
            # indicator isn't always False; fall back to the slug for manifest-less rows.
            has_offer=(bool(stem)
                       and (REPO_ROOT / "data" / "offers" / f"{stem}.json").is_file())
            or (REPO_ROOT / "data" / "offers" / f"{slug}.json").is_file(),
        )
        rows.append(row)

    return Snapshot(
        date=data.get("date", ""),
        profile=data.get("profile", ""),
        source=data.get("source", ""),
        count=data.get("count", len(rows)),
        rows=rows,
    )


def load_all_snapshots() -> list[tuple[str, Path]]:
    """Return [(date_str, path), ...] sorted oldest→newest."""
    snap_dir = REPO_ROOT / "data" / "snapshots"
    if not snap_dir.is_dir():
        return []
    pairs = []
    for p in sorted(snap_dir.glob("*.json")):
        stem = p.stem
        pairs.append((stem, p))
    return pairs


def load_favorites() -> dict[str, Any]:
    """Load the curated shortlist from config/favorites.json (PII-free), or {}."""
    path = REPO_ROOT / "config" / "favorites.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def load_doc_index() -> dict[str, list[dict]]:
    """Map a tariff stem → its persisted source-document descriptors (from the
    manifest), so the Favorites view can show which AVB/PIB URLs we have on file."""
    path = REPO_ROOT / "data" / "sources" / "check24-documents.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    index: dict[str, list[dict]] = {}
    for t in data.get("tariffs", []):
        if not isinstance(t, dict):
            continue
        stem = t.get("stem")
        if stem:
            docs = t.get("docs", [])
            index[stem] = docs if isinstance(docs, list) else []
    return index


def load_doc_by_tariff() -> dict[tuple[str, str], dict]:
    """Map (insurer, product) normalised → the full manifest tariff entry (stem +
    docs), so the Market view can resolve any selected row to its harvested source
    PDFs. The stems are hand-curated (e.g. "…-oerag") and not reproducible from a
    slug, so we match on the insurer/tariff strings the manifest itself records —
    they come from the same CHECK24 DOM as the snapshot rows."""
    path = REPO_ROOT / "data" / "sources" / "check24-documents.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    index: dict[tuple[str, str], dict] = {}
    for t in data.get("tariffs", []):
        if not isinstance(t, dict):
            continue
        ins = (t.get("insurer") or "").strip().casefold()
        prod = (t.get("tariff") or "").strip().casefold()
        if ins and prod:
            index[(ins, prod)] = t
    return index


# The URL manifest is static within a session (we never re-harvest URLs in-app), so
# cache the (insurer, product) -> entry map once for the module-level stem resolver.
_DOC_BY_TARIFF_CACHE: dict[tuple[str, str], dict] | None = None


def _doc_by_tariff_map() -> dict[tuple[str, str], dict]:
    global _DOC_BY_TARIFF_CACHE
    if _DOC_BY_TARIFF_CACHE is None:
        _DOC_BY_TARIFF_CACHE = load_doc_by_tariff()
    return _DOC_BY_TARIFF_CACHE


def resolve_stem(insurer: str, product: str) -> str | None:
    """Map a snapshot (insurer, product) to its canonical tariff stem via the URL
    manifest. Exact match first, then a unique product match with overlapping insurer
    names (handles the snapshot's 'BavariaDirekt' vs the manifest's
    'BavariaDirekt / ÖRAG'). This is the single source of truth for a row's identity."""
    idx = _doc_by_tariff_map()
    ins = insurer.strip().casefold()
    prod = product.strip().casefold()
    entry = idx.get((ins, prod))
    if entry is not None:
        return entry.get("stem")
    hits = [v for (i, p), v in idx.items() if p == prod and (ins in i or i in ins)]
    return hits[0].get("stem") if len(hits) == 1 else None


def _raw_dir_for_stem(stem: str) -> Path:
    """The canonical local-PDF directory for a stem (insurer__tariff -> insurer/tariff)."""
    insurer_part, _, tariff_part = stem.partition("__")
    return REPO_ROOT / "data" / "raw" / insurer_part / tariff_part


# Short doctype labels for the Favorites "Docs" column.
_DOCTYPE_SHORT = {
    "avb": "AVB",
    "produktinfoblatt": "PIB",
    "weitere_unterlagen": "Weit.",
}


def match_favorite(
    snapshot: Snapshot, fav: dict[str, Any]
) -> tuple[SnapshotRow | None, list[SnapshotRow]]:
    """Resolve one favorite to a representative snapshot row + all its SB variants.

    Matches on exact insurer + product. Picks the variant whose Selbstbeteiligung
    equals the favorite's ``show_sb`` (apples-to-apples band); falls back to the
    cheapest priced variant when that band is not in the snapshot.
    """
    ins = fav.get("insurer", "").strip()
    prod = fav.get("product", "").strip()
    variants = [
        r for r in snapshot.rows
        if r.insurer.strip() == ins and r.product.strip() == prod
    ]
    if not variants:
        return None, []
    show_sb = (fav.get("show_sb") or "").strip()
    chosen = None
    if show_sb:
        chosen = next(
            (r for r in variants if r.selbstbeteiligung.strip() == show_sb), None
        )
    if chosen is None:
        priced = [r for r in variants if r.monatlich_eur is not None]
        chosen = min(priced, key=lambda r: r.monatlich_eur) if priced else variants[0]
    return chosen, variants


# ---------------------------------------------------------------------------
# Feature-history helpers
# ---------------------------------------------------------------------------

def load_feature_diff(
    stem: str, old_date: str, new_date: str
) -> tuple[dict | None, dict | None, dict]:
    """Return (old_state, new_state, diff) for a stem between two dates.

    diff is {} when both states are present but identical, or when one/both
    states are missing (sparse coverage). Callers check old/new for None to
    distinguish "not yet analyzed" from "analyzed, no change".
    """
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        from feature_history import state_as_of, diff_features
    except ImportError:
        return None, None, {}
    old = state_as_of(stem, old_date)
    new = state_as_of(stem, new_date)
    if old is None or new is None:
        return old, new, {}
    return old, new, diff_features(old, new)


# ---------------------------------------------------------------------------
# Non-interactive selftest
# ---------------------------------------------------------------------------
def run_selftest(snapshot_path: Path | None) -> int:
    """Load data files, print summary, return exit code."""
    print("=== check0r3000 selftest ===")

    # 1. Resolve snapshot
    if snapshot_path is None:
        snap_dir = REPO_ROOT / "data" / "snapshots"
        latest = _find_latest_snapshot(snap_dir)
        if latest is None:
            print(f"[snapshots] directory missing or empty: {snap_dir}")
            print("  => no snapshot to test with")
        else:
            snapshot_path = latest
            print(f"[snapshots] found latest: {snapshot_path.name}")

    if snapshot_path is not None:
        snap = load_snapshot(snapshot_path)
        if snap is None:
            print(f"[snapshot] FAILED to parse: {snapshot_path}")
            return 1
        print(
            f"[snapshot] date={snap.date!r}  profile={snap.profile!r}  "
            f"count={snap.count}  rows_loaded={len(snap.rows)}"
        )
        tracked_in_snap = sum(1 for r in snap.rows if r.has_detail)
        offered_in_snap = sum(1 for r in snap.rows if r.has_offer)
        print(
            f"  rows with detail record: {tracked_in_snap}  "
            f"rows with personal offer: {offered_in_snap}"
        )
    else:
        print("[snapshot] (none loaded)")

    # 2. Detail records
    tariff_dir = REPO_ROOT / "out" / "tariffs"
    enriched_dir = REPO_ROOT / "out" / "enriched"
    n_tariffs = len(list(tariff_dir.glob("*.json"))) if tariff_dir.is_dir() else 0
    n_enriched = len(list(enriched_dir.glob("*.json"))) if enriched_dir.is_dir() else 0
    print(f"[tariffs]  out/tariffs/: {n_tariffs} files   out/enriched/: {n_enriched} files")

    # 3. Offers
    offer_dir = REPO_ROOT / "data" / "offers"
    n_offers = 0
    if offer_dir.is_dir():
        n_offers = len([p for p in offer_dir.glob("*.json") if not p.name.startswith("_")])
    print(f"[offers]   data/offers/: {n_offers} files")

    # 4. Snapshot diff
    all_snaps = load_all_snapshots()
    print(f"[diff]     snapshots available: {len(all_snaps)}")
    if len(all_snaps) >= 2:
        print("  diff view would be available")
    else:
        print("  only one snapshot — diff view shows empty-state message")

    # 5. Favorites board
    favs = load_favorites()
    fav_list = favs.get("favorites", [])
    doc_index = load_doc_index()
    print(f"[favorites] config/favorites.json: {len(fav_list)} entries")
    if fav_list and snapshot_path is not None:
        snap = load_snapshot(snapshot_path)
        unmatched = 0
        for fav in fav_list:
            row, variants = match_favorite(snap, fav) if snap else (None, [])
            n_docs = len(doc_index.get(fav.get("stem", ""), []))
            ins = (fav.get("insurer") or "")[:14]
            prod = (fav.get("product") or "")[:34]
            if row is not None:
                price = f"{row.monatlich_eur:.2f}" if row.monatlich_eur is not None else "—"
                print(
                    f"  ✓ {ins:<14} {prod:<34} "
                    f"note {row.tarifnote}  €{price:<7} SB {row.selbstbeteiligung:<14} "
                    f"({len(variants)} variants, {n_docs} docs)"
                )
            else:
                unmatched += 1
                print(f"  ! {ins:<14} {prod} — no snapshot match")
        if unmatched:
            # A favorite is curated config; the snapshot is regenerable data. An
            # unmatched favorite means the LIST is stale (or the snapshot drifted),
            # not that the loader is broken — warn, do not fail the loader selftest.
            print(f"  => {unmatched} favorite(s) unmatched — refresh favorites.json or the snapshot")

    print("=== selftest PASSED ===")
    return 0


if __name__ == "__main__":
    import sys

    # Textual-free data smoke test (replaces `python3 tui.py --selftest`).
    sys.exit(run_selftest(None))
