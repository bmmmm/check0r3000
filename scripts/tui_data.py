"""check0r3000 — data-loading layer.

Textual-free leaf module: snapshot/detail dataclasses and every load_* helper,
the manifest stem resolver, and the non-interactive selftest. Imported by tui.py
as a sibling; also importable (and runnable as `python3 tui_data.py`) under a
plain interpreter with no Textual installed, for a zero-dependency data smoke
test."""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# Sibling leaves (stdlib-only, no Textual): the coverage taxonomy is lru_cached and
# must be invalidated on reload, and _jsonio carries the shared JSON read helper.
import _vertical
import coverage_taxonomy
from _jsonio import load_json_or

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
                        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem))
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
    # Snapshot-wide customer-rating range, for the data-relative bewertung colour.
    bewertung_lo: float | None = None
    bewertung_hi: float | None = None


@dataclass
class ChangeInfo:
    """Aggregated change tracking for one stem: Leistungs- + Preisänderungen."""

    feature_changes: int            # number of detected Leistungs-changes
    price_changes: int              # number of detected snapshot price changes
    last_change_date: str | None    # most-recent change (feature or price), YYYY-MM-DD
    last_analysis_date: str | None  # most-recent re-analysis regardless of change
    first_seen_date: str | None     # date of the baseline history entry
    feature_changelog: list         # [(old_date, new_date, diff_dict), ...]
    price_changelog: list           # [{date, old_price, new_price, delta}, ...]
    # Full pinned-SB price series across all snapshots (change events or not):
    # [{date, price|None}, ...] oldest→newest — feeds the Preisverlauf sparkline.
    price_series: list = field(default_factory=list)


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
    # Hash of the extraction input (documents + prompt/model/filter/repeat). Two stems
    # sharing it were extracted from the SAME documents, so any difference between their
    # facts is model variance rather than a real product difference.
    input_hash: str | None = None


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
        input_hash=(data.get("_input_hash")
                    if isinstance(data.get("_input_hash"), str) else None),
    )


def _detail_path_for_stem(stem: str) -> tuple[Path, bool] | None:
    """Locate the analyzed record for a canonical stem (enriched preferred)."""
    for sub, is_enriched in (("enriched", True), ("tariffs", False)):
        path = _vertical.out_dir() / sub / f"{stem}.json"
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
        path = _vertical.out_dir() / sub / f"{slug}.json"
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
        directory = _vertical.out_dir() / sub
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
        _vertical.tariffs_dir(),
        _vertical.enriched_dir(),
        _vertical.offers_dir(),
    ]:
        if directory.is_dir():
            for p in directory.glob("*.json"):
                if not p.name.startswith("_"):
                    keys.add(p.stem)
    return keys


# Parsed + enriched snapshots, keyed by (resolved path, file mtime_ns): the Verlauf
# tab re-loads the same two historical snapshots on every ,/./m keypress, and a market
# filter keystroke re-loads the latest — all free once cached. The mtime key auto-
# invalidates on a rewritten file. The enrichment (stem/has_detail/…) also depends on
# external state (the doc manifest, out/tariffs, data/offers), so reset_doc_cache()
# clears this too — it runs on every data reload, which is exactly when that state can
# change (a fresh extract flips has_detail without touching the snapshot file).
_SNAPSHOT_CACHE: dict[tuple[str, int], "Snapshot"] = {}


def load_snapshot(path: Path) -> Snapshot | None:
    """Load a snapshot JSON. Returns None if file missing or malformed.

    Cached by (path, mtime); callers only read the result (no row mutation — the market
    sort copies rows into a fresh list first), so the shared instance is safe."""
    if not path.is_file():
        return None
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return None
    cache_key = (str(path.resolve()), mtime)
    cached = _SNAPSHOT_CACHE.get(cache_key)
    if cached is not None:
        return cached
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
                       and (_vertical.offers_dir() / f"{stem}.json").is_file())
            or (_vertical.offers_dir() / f"{slug}.json").is_file(),
        )
        rows.append(row)

    bews = [r.bewertung for r in rows if r.bewertung is not None]
    snap = Snapshot(
        date=data.get("date", ""),
        profile=data.get("profile", ""),
        source=data.get("source", ""),
        count=data.get("count", len(rows)),
        rows=rows,
        bewertung_lo=min(bews) if bews else None,
        bewertung_hi=max(bews) if bews else None,
    )
    _SNAPSHOT_CACHE[cache_key] = snap
    return snap


def load_all_snapshots() -> list[tuple[str, Path]]:
    """Return [(date_str, path), ...] sorted oldest→newest.

    Only date-named files (YYYY-MM-DD.json, same pattern as _find_latest_snapshot)
    are included -- a stray non-date *.json in data/snapshots/ (a backup, an
    export) would otherwise masquerade as a snapshot and make every tracked
    tariff look removed in the Verlauf diff."""
    import re

    snap_dir = _vertical.snapshots_dir()
    if not snap_dir.is_dir():
        return []
    pairs = []
    for p in sorted(snap_dir.glob("*.json")):
        stem = p.stem
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", stem):
            continue
        pairs.append((stem, p))
    return pairs


def load_favorites() -> dict[str, Any]:
    """Load the curated shortlist from config/favorites.json (PII-free), or {}."""
    path = _vertical.favorites_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def load_favorite_notes() -> dict[str, str]:
    """Load per-stem favorite notes from the gitignored sidecar
    config/favorite-notes.json, or {}.

    Notes are free text a user types via [N] — personal, not shareable — so they
    live OUT of the tracked (stem/tag/SB-only, PII-free) favorites.json. Keyed by the
    canonical tariff stem, merged over the favorites at render time."""
    data = load_json_or(_vertical.favorite_notes_path(), {})
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if v}


def load_doc_index() -> dict[str, list[dict]]:
    """Map a tariff stem → its persisted source-document descriptors (from the
    manifest), so the Favorites view can show which AVB/PIB URLs we have on file."""
    path = _vertical.manifest_path()
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
    path = _vertical.manifest_path()
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


def reset_doc_cache() -> None:
    """Invalidate the module-level (insurer, product) -> entry cache behind
    resolve_stem. The URL manifest IS rewritten in-session — [H] live-harvest
    appends new stems — so the cache must be dropped on every data reload, or a
    freshly-harvested tariff keeps stem=None (invisible to detail/Vergleich/
    change-tracking) until the app restarts. Also drops the parsed-snapshot cache,
    whose row enrichment (stem/has_detail/…) is derived from this same manifest and
    the on-disk record state that a reload may have changed."""
    global _DOC_BY_TARIFF_CACHE
    _DOC_BY_TARIFF_CACHE = None
    _SNAPSHOT_CACHE.clear()
    # The coverage taxonomy is lru_cached; drop it too so an edit to
    # config/coverage_taxonomy.json mid-session takes effect on the next reload
    # instead of staying invisible until an app restart.
    coverage_taxonomy.load_taxonomy.cache_clear()


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
    return _vertical.raw_dir() / insurer_part / tariff_part


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
# Change-summary: feature history + price history combined
# ---------------------------------------------------------------------------

def load_change_summary() -> dict[str, ChangeInfo]:
    """Build per-stem change info by merging feature_history and price_history.

    Loads all tariff-history dirs and all snapshot price series. Returned dict
    contains only stems for which at least one data source has an entry.
    """
    import sys as _sys
    _scripts = str(REPO_ROOT / "scripts")
    if _scripts not in _sys.path:
        _sys.path.insert(0, _scripts)

    try:
        import feature_history as fh
        import price_history as ph
    except ImportError:
        return {}

    result: dict[str, ChangeInfo] = {}

    # 1. Feature history — one sub-dir per stem. Wrapped like the price half so an
    #    unexpected error (e.g. an iterdir permission failure) can't abort _load_data.
    hist_dir = _vertical.history_dir()
    if hist_dir.is_dir():
        try:
            for stem_dir in sorted(hist_dir.iterdir()):
                if not stem_dir.is_dir():
                    continue
                stem = stem_dir.name
                # Count actual feature diffs, not archived versions. archive_version
                # writes a new version whenever the content hash changes — including a
                # `stand`-only change (an LLM-extracted, run-to-run-variant string that
                # diff_features deliberately ignores), which would otherwise surface as
                # "Δ1 / 1 Änderung" with no listed change. full_changelog yields only
                # pairs with a real diff, so len()/its last date are the honest figures.
                changelog = fh.full_changelog(stem)
                result[stem] = ChangeInfo(
                    feature_changes=len(changelog),
                    price_changes=0,
                    last_change_date=changelog[-1][1] if changelog else None,
                    last_analysis_date=fh.last_analysis_date(stem),
                    first_seen_date=fh.first_seen_date(stem),
                    feature_changelog=changelog,
                    price_changelog=[],
                )
        except OSError:
            pass

    # 2. Price history — derive from all snapshots via resolve_stem
    try:
        price_hist = ph.load_all_price_history(resolve_stem)
    except Exception:
        price_hist = {}

    for stem, series in price_hist.items():
        pcl = ph.price_changelog(series)
        pc = len(pcl)
        lcd_price = ph.last_price_change_date(series)
        if stem in result:
            ci = result[stem]
            dates = [d for d in [ci.last_change_date, lcd_price] if d]
            result[stem] = ChangeInfo(
                feature_changes=ci.feature_changes,
                price_changes=pc,
                last_change_date=max(dates) if dates else None,
                last_analysis_date=ci.last_analysis_date,
                first_seen_date=ci.first_seen_date,
                feature_changelog=ci.feature_changelog,
                price_changelog=pcl,
                price_series=series,
            )
        elif pc > 0 or len([e for e in series if e.get("price") is not None]) >= 2:
            result[stem] = ChangeInfo(
                feature_changes=0,
                price_changes=pc,
                last_change_date=lcd_price,
                last_analysis_date=None,
                first_seen_date=None,
                feature_changelog=[],
                price_changelog=pcl,
                price_series=series,
            )

    return result


def dominant_provenance() -> tuple[str | None, bool, int]:
    """(model, filter_on, repeat) shared by the most out/tariffs records.

    The extract cache signature includes all three — an Update-All that extracts
    with a different spec silently re-extracts EVERY tariff at cost and replaces
    union-of-N records with single runs. Deriving the flags from the records
    themselves keeps the refresh cache-current by construction.
    (None, False, 1) when no records exist yet."""
    from collections import Counter

    combos: Counter = Counter()
    tdir = _vertical.tariffs_dir()
    if tdir.is_dir():
        for p in sorted(tdir.glob("*.json")):
            d = load_json_or(p, None)
            if not isinstance(d, dict):
                continue
            rep = d.get("_repeat")
            combos[(
                d.get("_model") or None,
                bool(d.get("_filter")),
                rep if isinstance(rep, int) and rep >= 1 else 1,
            )] += 1
    if not combos:
        return None, False, 1
    return combos.most_common(1)[0][0]


def load_market_stats() -> list[dict]:
    """Per-snapshot market aggregates from price_history.market_stats().

    Same lazy-import + swallow-errors contract as load_change_summary: the
    Verlauf header degrades to no stats line rather than crashing the TUI."""
    import sys as _sys
    _scripts = str(REPO_ROOT / "scripts")
    if _scripts not in _sys.path:
        _sys.path.insert(0, _scripts)
    try:
        import price_history as ph
        return ph.market_stats()
    except Exception:
        return []


# ---------------------------------------------------------------------------
# External ratings (Finanztip / Franke & Bornberg / Finanztest) — display only
# ---------------------------------------------------------------------------

def load_external_ratings() -> dict[str, Any]:
    """Hand-curated external test verdicts from data/sources/external-ratings.json.

    Sparse by nature (external tests cover a fraction of the market), so these
    are DISPLAY-ONLY — never a score input, same rule as price in Magic Find."""
    data = load_json_or(_vertical.external_ratings_path(), {})
    return data if isinstance(data, dict) else {}


def _rating_tokens(insurer: str, stem: str | None) -> set[str]:
    """Whole-word tokens for insurer-level rating matches: the umlaut-normalised
    insurer name plus every stem word (the stem carries risk-carrier names like
    'oerag' that the display insurer may not, e.g. S-Direkt selling ÖRAG)."""
    import re

    text = (insurer or "").casefold()
    for uml, ascii_ in (("ö", "oe"), ("ä", "ae"), ("ü", "ue"), ("ß", "ss")):
        text = text.replace(uml, ascii_)
    if stem:
        text += " " + stem.casefold()
    return {t for t in re.split(r"[^a-z0-9]+", text) if t}


def external_ratings_for(
    stem: str | None, insurer: str = "", data: dict[str, Any] | None = None
) -> list[dict]:
    """Tariff-level entries for the stem + insurer-level entries whose key matches
    a whole word of the insurer name or stem. Whole-word matching is deliberate:
    a substring test would let 'arag' hit 'oerag'. A stem listed in
    tariff_aliases inherits its base stem's verdicts (product-identical
    variant), marked with via=<base> so the display can say so."""
    if data is None:
        data = load_external_ratings()
    out: list[dict] = []
    tariffs = data.get("tariffs")
    if isinstance(tariffs, dict) and stem:
        entries = tariffs.get(stem)
        if not entries:
            aliases = data.get("tariff_aliases")
            base = aliases.get(stem) if isinstance(aliases, dict) else None
            if base:
                entries = [{**e, "via": base} for e in tariffs.get(base) or []
                           if isinstance(e, dict)]
        if isinstance(entries, list):
            out += [e for e in entries if isinstance(e, dict)]
    insurers = data.get("insurers")
    if isinstance(insurers, dict):
        tokens = _rating_tokens(insurer, stem)
        for key, entries in insurers.items():
            if str(key).casefold() in tokens and isinstance(entries, list):
                out += [e for e in entries if isinstance(e, dict)]
    return out


def external_market_notes(data: dict[str, Any] | None = None) -> list[dict]:
    """Externally recommended tariffs that are NOT in the CHECK24 market at all
    (direct sellers like WGV/HUK-Coburg) — the tool's structural blind spot,
    surfaced as a Magic-Find note instead of being silently invisible."""
    if data is None:
        data = load_external_ratings()
    notes = data.get("_market_notes")
    if not isinstance(notes, list):
        return []
    return [n for n in notes if isinstance(n, dict)]


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
    _scripts = str(REPO_ROOT / "scripts")
    if _scripts not in _sys.path:
        _sys.path.insert(0, _scripts)
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
# Subprocess streaming (live pipeline progress)
# ---------------------------------------------------------------------------
@dataclass
class StreamResult:
    """Outcome of stream_subprocess. ok iff the child spawned, finished within the
        timeout, and exited 0. tail holds the last few non-empty output lines."""

    returncode: int | None
    timed_out: bool
    tail: list[str]
    spawn_error: str | None = None

    @property
    def ok(self) -> bool:
        return (
            self.spawn_error is None
            and not self.timed_out
            and self.returncode == 0
        )

    @property
    def reason(self) -> str:
        """A short failure reason (empty string when ok)."""
        if self.spawn_error is not None:
            return self.spawn_error
        if self.timed_out:
            return "timeout"
        if self.returncode not in (0, None):
            return self.tail[-1] if self.tail else f"exit {self.returncode}"
        return ""


def stream_subprocess(
    cmd: list[str],
    cwd: Path,
    timeout: int,
    log_path: Path | None = None,
    on_line: Callable[[str], None] | None = None,
    *,
    tail_n: int = 8,
    throttle_s: float = 0.2,
) -> StreamResult:
    """Run cmd, streaming merged stdout+stderr line by line. Appends the full
        output to log_path (if given) and reports the latest non-empty line to
        on_line (throttled to throttle_s) for a live status display.

        A threading.Timer enforces timeout even when the child produces no output:
        a plain `for line in proc.stdout` loop blocks on readline() and would never
        notice a silent hang. Never raises for a child failure — a spawn error is
        captured in StreamResult.spawn_error so the caller can report it."""
    import subprocess

    try:
        proc = subprocess.Popen(
            cmd, cwd=str(cwd),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
    except OSError as exc:
        return StreamResult(returncode=None, timed_out=False, tail=[],
                            spawn_error=str(exc))

    tail: deque[str] = deque(maxlen=tail_n)
    timed_out = threading.Event()

    def _kill_on_timeout() -> None:
        # Only flag a timeout if the child is STILL running when the timer fires.
        # The timer can fire in the hairline window between proc.wait() returning
        # and timer.cancel() below; a process that finished in time must not be
        # reported as timed out. poll() is None iff the child is still alive, so
        # the kill (and the flag) happen only when the timer actually ends it.
        if proc.poll() is None:
            timed_out.set()
            proc.kill()

    timer = threading.Timer(timeout, _kill_on_timeout)
    timer.start()
    last_push = 0.0
    logf = None
    try:
        if log_path is not None:
            try:
                logf = open(log_path, "a", encoding="utf-8")
                logf.write(f"\n===== {' '.join(cmd)} =====\n")
            except OSError:
                logf = None
        assert proc.stdout is not None
        for raw in proc.stdout:
            if logf is not None:
                logf.write(raw)
            s = raw.strip()
            if not s:
                continue
            tail.append(s)
            if on_line is not None:
                now = time.monotonic()
                if now - last_push > throttle_s:
                    last_push = now
                    on_line(s)
        proc.wait()
    finally:
        timer.cancel()
        if proc.stdout:
            proc.stdout.close()
        if logf is not None:
            logf.close()

    return StreamResult(
        returncode=proc.returncode,
        timed_out=timed_out.is_set(),
        tail=list(tail),
    )


def _selftest_stream() -> list[str]:
    """Exercise stream_subprocess offline (no Textual, no network). Returns a list
        of failure strings — empty means pass."""
    import subprocess  # noqa: F401  (sys.executable drives the child below)
    import sys
    import tempfile

    fails: list[str] = []
    py = sys.executable
    tmp = Path(tempfile.mkdtemp(prefix="check0r-stream-"))

    # 1. success: two stdout lines, exit 0 — log + on_line + tail populated.
    seen: list[str] = []
    log1 = tmp / "ok.log"
    res = stream_subprocess(
        [py, "-c", "print('line one'); print('line two')"],
        REPO_ROOT, 30, log1, on_line=seen.append, throttle_s=0.0,
    )
    if not res.ok:
        fails.append(f"stream ok: expected ok, got {res!r}")
    if res.tail[-1:] != ["line two"]:
        fails.append(f"stream ok: tail tail wrong: {res.tail}")
    if not seen:
        fails.append("stream ok: on_line never fired")
    body = log1.read_text(encoding="utf-8")
    if "line one" not in body or "line two" not in body:
        fails.append("stream ok: log missing streamed lines")

    # 2. failure: non-zero exit, reason carries the last output line.
    res = stream_subprocess(
        [py, "-c", "print('boom'); raise SystemExit(3)"], REPO_ROOT, 30,
    )
    if res.ok or res.returncode != 3:
        fails.append(f"stream fail: expected exit 3, got {res!r}")
    if res.reason != "boom":
        fails.append(f"stream fail: reason wrong: {res.reason!r}")

    # 3. timeout: silent child (no output) must still be killed by the Timer.
    res = stream_subprocess(
        [py, "-c", "import time; time.sleep(30)"], REPO_ROOT, 1,
    )
    if not res.timed_out or res.ok or res.reason != "timeout":
        fails.append(f"stream timeout: expected timeout, got {res!r}")

    # 4. spawn error: a missing binary is captured, not raised.
    res = stream_subprocess(["check0r-no-such-binary-xyz"], REPO_ROOT, 5)
    if res.ok or res.spawn_error is None:
        fails.append(f"stream spawn: expected spawn_error, got {res!r}")

    for p in (tmp / "ok.log",):
        p.unlink(missing_ok=True)
    tmp.rmdir()
    return fails


# ---------------------------------------------------------------------------
# Non-interactive selftest
# ---------------------------------------------------------------------------
def run_selftest(snapshot_path: Path | None) -> int:
    """Load data files, print summary, return exit code."""
    print("=== check0r3000 selftest ===")

    # 1. Resolve snapshot
    if snapshot_path is None:
        snap_dir = _vertical.snapshots_dir()
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
    tariff_dir = _vertical.tariffs_dir()
    enriched_dir = _vertical.enriched_dir()
    n_tariffs = len(list(tariff_dir.glob("*.json"))) if tariff_dir.is_dir() else 0
    n_enriched = len(list(enriched_dir.glob("*.json"))) if enriched_dir.is_dir() else 0
    print(f"[tariffs]  out/tariffs/: {n_tariffs} files   out/enriched/: {n_enriched} files")

    # 3. Offers
    offer_dir = _vertical.offers_dir()
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

    # 6. Subprocess streaming core (drives the live pipeline status line).
    stream_fails = _selftest_stream()
    if stream_fails:
        print(f"[stream]   stream_subprocess: {len(stream_fails)} FAILURE(S)")
        for msg in stream_fails:
            print(f"  ✗ {msg}")
        print("=== selftest FAILED ===")
        return 1
    print("[stream]   stream_subprocess: ok (success/fail/timeout/spawn)")

    print("=== selftest PASSED ===")
    return 0


if __name__ == "__main__":
    import sys

    if "--provenance" in sys.argv[1:]:
        # Machine-readable dominant record provenance for update-all.sh:
        # "<model>|<0/1 filter>|<repeat>" — empty model when no records exist.
        m, f, r = dominant_provenance()
        print(f"{m or ''}|{1 if f else 0}|{r}")
        sys.exit(0)

    # Textual-free data smoke test (replaces `python3 tui.py --selftest`).
    sys.exit(run_selftest(None))
