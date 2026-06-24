#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["textual>=0.61.0"]
# ///
"""
check0r3000 — Rechtsschutz-Vergleich
Interactive TUI for browsing German legal-protection-insurance tariff comparisons.

Usage:
    uv run scripts/tui.py                         # launch interactive UI
    uv run scripts/tui.py --snapshot PATH         # load a specific snapshot JSON
    uv run scripts/tui.py --selftest              # verify data loading, then exit 0
    uv run scripts/tui.py --screenshot DIR        # render each tab to SVG, then exit
    uv run scripts/tui.py --help                  # show this help

Navigate a tariff (Market or Favorites) with the arrow keys or a click; press [d]
to toggle a detail band below the table (tariff modules, coverage, premium and the
harvested source documents). If the documents are not yet analyzed, [g] downloads
them and runs the pipeline (fetch_docs --into-raw -> ingest -> extract) in the
background, after a confirm. The extract model defaults to "claude"; override
with the CHECK0R_ANALYZE_MODEL env var. Tariffs whose URLs were never harvested
point you back to the browser "Tarifdetails" step.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Repo layout
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent

# Model spec for the [g] "download + analyze" pipeline's extract stage. Matches
# extract.py's own default ("claude" = the claude CLI); override without editing
# code via CHECK0R_ANALYZE_MODEL (e.g. a local mlx:/ollama: spec).
ANALYZE_MODEL = os.environ.get("CHECK0R_ANALYZE_MODEL", "claude")


# ---------------------------------------------------------------------------
# Data loading helpers
# ---------------------------------------------------------------------------


def _find_latest_snapshot(snapshot_dir: Path) -> Path | None:
    """Return the most-recent snapshot JSON by filename date, or None."""
    if not snapshot_dir.is_dir():
        return None
    candidates = sorted(snapshot_dir.glob("*.json"))
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
        s = re.sub(r"[äöü]", lambda m: {"ä": "ae", "ö": "oe", "ü": "ue"}[m.group()], s)
        s = re.sub(r"[^a-z0-9]+", "-", s)
        s = s.strip("-")
        return s

    return f"{slugify(insurer)}__{slugify(product)}"


def _record_from_data(
    data: dict, is_enriched: bool, insurer: str, product: str
) -> DetailRecord:
    return DetailRecord(
        insurer=data.get("insurer", insurer),
        tariff=data.get("tariff", product),
        stand=data.get("stand"),
        modules=data.get("modules", {}),
        coverage=data.get("coverage", {}),
        leistungen=data.get("leistungen", []),
        ausschluesse=data.get("ausschluesse", []),
        besonderheiten=data.get("besonderheiten", []),
        beitrag=data.get("beitrag"),
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

    tracked = _tracked_keys()
    rows: list[SnapshotRow] = []
    for t in data.get("tariffs", []):
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
            stem=stem,
            has_urls=stem is not None,
            has_pdf=bool(stem and _raw_dir_for_stem(stem).is_dir()),
            has_detail=has_detail,
            has_offer=(REPO_ROOT / "data" / "offers" / f"{slug}.json").is_file(),
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
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


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
    index: dict[str, list[dict]] = {}
    for t in data.get("tariffs", []):
        stem = t.get("stem")
        if stem:
            index[stem] = t.get("docs", [])
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
    index: dict[tuple[str, str], dict] = {}
    for t in data.get("tariffs", []):
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


# Data-availability status, from "we only read the listing" to "fully analyzed".
# The glyphs are explained by STATUS_LEGEND, shown above the Market table.
def _status_glyph(row: SnapshotRow) -> str:
    if row.has_detail:
        return "[bright_green]✓[/bright_green]"
    if row.has_pdf:
        return "[cyan]↓[/cyan]"
    if row.has_urls:
        return "[yellow]○[/yellow]"
    return "[dim]·[/dim]"


STATUS_LEGEND = (
    "[bold]Status[/bold]  [bright_green]✓[/bright_green] analysiert   "
    "[cyan]↓[/cyan] PDF lokal   [yellow]○[/yellow] URLs (\\[g] lädt + analysiert)   "
    "[dim]·[/dim] nur gelistet"
)


def _price_quartiles(rows: list[SnapshotRow]) -> tuple[float, float, float]:
    """Return (q1, median, q3) for monatlich_eur, ignoring None."""
    prices = sorted(r.monatlich_eur for r in rows if r.monatlich_eur is not None)
    if not prices:
        return (0, 0, 0)
    n = len(prices)
    q1 = prices[n // 4]
    median = prices[n // 2]
    q3 = prices[3 * n // 4]
    return q1, median, q3


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


# ---------------------------------------------------------------------------
# Textual imports (only needed for interactive mode)
# ---------------------------------------------------------------------------


def _launch_app(snapshot_path: Path | None, screenshot_dir: Path | None = None) -> None:
    from textual import on, work
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Container, ScrollableContainer, Vertical
    from textual.css.query import NoMatches
    from textual.reactive import reactive
    from textual.screen import ModalScreen
    from textual.widgets import (
        DataTable,
        Footer,
        Header,
        Input,
        Label,
        Static,
        TabbedContent,
        TabPane,
    )

    # -----------------------------------------------------------------------
    # Helpers used inside the app
    # -----------------------------------------------------------------------

    def _tarifnote_color(note: str) -> str:
        """Return a Rich color name based on Tarifnote value."""
        try:
            val = float(note.replace(",", "."))
        except (ValueError, AttributeError):
            return "white"
        if val <= 1.3:
            return "bright_green"
        if val <= 2.0:
            return "yellow"
        return "bright_red"

    def _price_color(price: float | None, q1: float, q3: float) -> str:
        if price is None:
            return "white"
        if price <= q1:
            return "bright_green"
        if price >= q3:
            return "bright_red"
        return "white"

    def _module_badge(mod: dict[str, Any]) -> str:
        if not mod.get("included"):
            return "[dim]—[/dim]"
        level = mod.get("level")
        if level == "Premium":
            return "[bright_green]★★★ Premium[/bright_green]"
        if level == "Komfort":
            return "[yellow]★★ Komfort[/yellow]"
        if level == "Basis":
            return "[white]★ Basis[/white]"
        return "[cyan]✓[/cyan]"

    MODULE_LABELS = {
        "privat": "Privat",
        "beruf": "Beruf",
        "verkehr": "Verkehr",
        "wohnen_immobilien": "Wohnen",
        "internet_web": "Internet",
        "steuer": "Steuer",
        "sozialgericht": "Sozialgericht",
        "verwaltungsrecht": "Verwaltungsrecht",
    }

    # -----------------------------------------------------------------------
    # Diff helper
    # -----------------------------------------------------------------------

    def _compute_diff(
        old_snap: Snapshot, new_snap: Snapshot
    ) -> tuple[list[tuple], list[str], list[str]]:
        """
        Returns (changes, added_keys, removed_keys).
        changes = [(key, old_price, new_price, delta), ...]
        """
        old_map = {r.key: r for r in old_snap.rows}
        new_map = {r.key: r for r in new_snap.rows}

        changes = []
        for key, new_row in new_map.items():
            if key in old_map:
                old_row = old_map[key]
                old_p = old_row.monatlich_eur
                new_p = new_row.monatlich_eur
                if old_p != new_p and old_p is not None and new_p is not None:
                    changes.append((key, old_p, new_p, new_p - old_p))

        added = [k for k in new_map if k not in old_map]
        removed = [k for k in old_map if k not in new_map]
        return changes, added, removed

    # -----------------------------------------------------------------------
    # The App
    # -----------------------------------------------------------------------

    class ConfirmFetchScreen(ModalScreen[bool]):
        """Deliberate gate before downloading third-party source PDFs and running
        the analyze pipeline. Returns True on confirm, False on cancel."""

        BINDINGS = [
            Binding("enter", "confirm", "Download + analyze"),
            Binding("y", "confirm", "Yes"),
            Binding("escape", "cancel", "Cancel"),
            Binding("n", "cancel", "No"),
        ]

        def __init__(self, entry: dict, model: str) -> None:
            super().__init__()
            self._entry = entry
            self._model = model

        def compose(self) -> ComposeResult:
            e = self._entry
            docs = e.get("docs", [])
            lines = [
                f"[bold]{e.get('insurer', '')} — {e.get('tariff', '')}[/bold]",
                "",
                f"Download von [cyan]rechtsschutz.check24.de[/cyan]: "
                f"[bold]{len(docs)}[/bold] PDF(s)",
            ]
            for dd in docs:
                lbl = _DOCTYPE_SHORT.get(dd.get("doctype", ""), dd.get("doctype", ""))
                lines.append(f"  • [cyan]{lbl:<6}[/cyan] {(dd.get('file') or '')[:48]}")
            lines += [
                "",
                f"dann: intake → ingest → extract  [dim](Modell: {self._model})[/dim]",
                "[dim]Drittanbieter-Copyright — nur für den Eigengebrauch.[/dim]",
                "",
                "[bold]\\[↵/y][/bold] Download + Analyse     [bold]\\[Esc/n][/bold] Abbrechen",
            ]
            yield Container(Static("\n".join(lines)), id="confirm-box")

        def action_confirm(self) -> None:
            self.dismiss(True)

        def action_cancel(self) -> None:
            self.dismiss(False)

    class DeleteDataScreen(ModalScreen[str | None]):
        """Pick how much of a tariff's local data to delete. Returns the chosen scope
        ('records' | 'purge' | 'purge_unfav') or None on cancel."""

        BINDINGS = [
            Binding("1", "pick('records')", "Records"),
            Binding("2", "pick('purge')", "Purge"),
            Binding("3", "pick('purge_unfav')", "Purge+Unfav"),
            Binding("escape", "cancel", "Cancel"),
            Binding("n", "cancel", "Cancel"),
        ]

        def __init__(self, stem: str, label: str, is_fav: bool) -> None:
            super().__init__()
            self._stem = stem
            self._label = label
            self._is_fav = is_fav

        def compose(self) -> ComposeResult:
            i, _, t = self._stem.partition("__")
            unfav = "" if self._is_fav else "   [dim](nicht in Favoriten)[/dim]"
            lines = [
                f"[bold]Daten löschen — {self._label}[/bold]",
                f"[dim]stem: {self._stem}[/dim]",
                "[dim]Irreversibel.[/dim]",
                "",
                "[bold]\\[1][/bold] Nur Analyse-Records",
                f"     [dim]out/tariffs|enriched/{self._stem}.json — per \\[g] neu erzeugbar[/dim]",
                "[bold]\\[2][/bold] Records + lokale PDFs + Texte",
                f"     [dim]+ data/raw/{i}/{t}/ + data/extracted/{i}/{t}/ — PDFs neu zu laden[/dim]",
                f"[bold]\\[3][/bold] Voller Purge + aus Favoriten entfernen{unfav}",
                "",
                "[bold]\\[Esc/n][/bold] Abbrechen",
            ]
            yield Container(Static("\n".join(lines)), id="confirm-box")

        def action_pick(self, scope: str) -> None:
            self.dismiss(scope)

        def action_cancel(self) -> None:
            self.dismiss(None)

    class QueryUrlScreen(ModalScreen[None]):
        """Show the decoded CHECK24 query levers and where the full result URLs were
        written, for the manual browser + scrape-snippet workflow."""

        BINDINGS = [
            Binding("escape", "close", "Close"),
            Binding("enter", "close", "Close"),
            Binding("q", "close", "Close"),
        ]

        def __init__(self, levers: str, url_file: str, is_example: bool) -> None:
            super().__init__()
            self._levers = levers
            self._url_file = url_file
            self._is_example = is_example

        def compose(self) -> ComposeResult:
            lines = [
                "[bold]CHECK24-Query bauen[/bold]",
                "[dim]Im Browser öffnen → scripts/check24_scrape.js in die DevTools-"
                "Konsole einfügen → snapshot.py / check24Docs.[/dim]",
                "",
                f"[underline]URLs geschrieben[/underline]: [cyan]{self._url_file}[/cyan]",
                "[dim]   (gespeicherte Query + Variante 'alle Versicherer')[/dim]",
            ]
            if self._levers:
                lines += ["", "[underline]Levers[/underline]", self._levers.replace("[", "\\[")]
            if self._is_example:
                lines += [
                    "",
                    "[yellow]! Beispielprofil (Fake-Daten) — config/check24-profile.json "
                    "anlegen.[/yellow]",
                ]
            lines += ["", "[bold]\\[Esc][/bold] Schließen"]
            yield Container(Static("\n".join(lines)), id="query-box")

        def action_close(self) -> None:
            self.dismiss(None)

    class CheckApp(App):
        """check0r3000 — Rechtsschutz-Vergleich TUI."""

        CSS_PATH = Path(__file__).parent / "tui.tcss"
        TITLE = "check0r3000 — Rechtsschutz-Vergleich"

        BINDINGS = [
            Binding("q", "quit", "Quit"),
            Binding("f", "focus_filter", "Filter", show=True),
            Binding("escape", "clear_filter", "Clear filter"),
            Binding("s", "sort_price", "Sort €", show=True),
            Binding("n", "sort_note", "Sort note", show=True),
            Binding("p", "sort_position", "Sort #", show=True),
            Binding("v", "switch_tab('favorites')", "Favorites", show=True),
            Binding("m", "switch_tab('market')", "Market", show=True),
            Binding("d", "toggle_detail", "Details", show=True),
            Binding("x", "switch_tab('diff')", "Diff", show=True),
            Binding("g", "fetch_docs", "Get docs", show=True),
            Binding("b", "build_query", "Query-URL", show=False),
            Binding("u", "toggle_favorite", "Favorit", show=False),
            Binding("R", "set_reference", "Referenz", show=False),
            Binding("D", "delete_data", "Daten löschen", show=False),
            Binding("r", "refresh_data", "Reload", show=False),
        ]

        # reactive state
        filter_text: reactive[str] = reactive("", recompose=False)
        sort_col: reactive[str] = reactive("position")
        sort_asc: reactive[bool] = reactive(True)
        selected_row_key: reactive[str | None] = reactive(None)

        def __init__(self, snapshot_path: Path | None) -> None:
            super().__init__()
            self._snapshot_path = snapshot_path
            self._snapshot: Snapshot | None = None
            self._all_snapshots: list[tuple[str, Path]] = []
            self._detail: DetailRecord | None = None
            self._q1 = self._median = self._q3 = 0.0
            self._favorites: dict[str, Any] = {}
            self._doc_index: dict[str, list[dict]] = {}
            self._doc_by_tariff: dict[tuple[str, str], dict] = {}
            self._doc_by_stem: dict[str, dict] = {}
            self._fav_rows: dict[str, tuple[SnapshotRow, dict]] = {}
            self._active_row: SnapshotRow | None = None
            self._active_fav: dict | None = None

        # --- Lifecycle ---

        def on_mount(self) -> None:
            self._load_data()
            self._populate_favorites_table()
            self._populate_market_table()
            self._update_header()

        def _load_data(self) -> None:
            """Load snapshot and supplemental data."""
            if self._snapshot_path is not None:
                path = self._snapshot_path
            else:
                path = _find_latest_snapshot(REPO_ROOT / "data" / "snapshots")

            if path is not None:
                self._snapshot = load_snapshot(path)

            self._all_snapshots = load_all_snapshots()
            if self._snapshot:
                self._q1, self._median, self._q3 = _price_quartiles(self._snapshot.rows)
            self._favorites = load_favorites()
            self._doc_index = load_doc_index()
            self._doc_by_tariff = load_doc_by_tariff()
            self._doc_by_stem = {
                t["stem"]: t for t in self._doc_by_tariff.values() if t.get("stem")
            }

        # --- Layout ---

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            with TabbedContent(id="tabs", initial="favorites"):
                with TabPane("★ Favorites [v]", id="favorites"):
                    yield Label("", id="fav-knockout")
                    with Vertical(id="fav-layout"):
                        yield DataTable(id="fav-table", cursor_type="row", zebra_stripes=True)
                        with ScrollableContainer(id="fav-detail", classes="detail-band"):
                            yield Static(
                                "Select a favorite to see full details, SB variants and documents.",
                                id="fav-detail-content",
                            )
                with TabPane("Market [m]", id="market"):
                    yield Input(
                        placeholder="Filter by insurer or product…",
                        id="filter-input",
                    )
                    yield Label(STATUS_LEGEND, id="market-legend")
                    with Vertical(id="market-layout"):
                        yield DataTable(id="market-table", cursor_type="row", zebra_stripes=True)
                        with ScrollableContainer(id="detail-panel", classes="detail-band"):
                            yield Static("Select a row to see details.", id="detail-content")
                with TabPane("Diff [x]", id="diff"):
                    with ScrollableContainer(id="diff-panel"):
                        yield Static("Loading diff…", id="diff-content")
            yield Footer()

        # --- Header update ---

        def _update_header(self) -> None:
            if self._snapshot:
                self.sub_title = (
                    f"{self._snapshot.date}  |  {self._snapshot.profile}"
                    f"  |  {len(self._snapshot.rows)} tariffs"
                )
            else:
                self.sub_title = "No snapshot loaded — place files in data/snapshots/"

        # --- Favorites board ---

        def _is_reference(self, fav: dict) -> bool:
            """A favorite is the reference iff its stem is the configured reference_stem.
            (reference_stem is the single source of truth — set live with [R].)"""
            rs = self._favorites.get("reference_stem")
            return bool(rs and fav.get("stem") == rs)

        def _reference_sb(self) -> str:
            """The reference SB band — explicit reference_sb, else the reference
            favorite's show_sb (back-compat with the pre-[R] config)."""
            sb = (self._favorites.get("reference_sb") or "").strip()
            if sb:
                return sb
            rs = self._favorites.get("reference_stem")
            rf = next(
                (f for f in self._favorites.get("favorites", []) if f.get("stem") == rs), None
            )
            return (rf.get("show_sb") or "").strip() if rf else ""

        def _reference_row(self) -> SnapshotRow | None:
            """The snapshot row that is the comparison baseline. Resolves
            reference_stem against the snapshot (so the reference need NOT be a
            favorite), picking the reference SB band, else the cheapest variant."""
            if not self._snapshot:
                return None
            rs = self._favorites.get("reference_stem")
            if not rs:
                return None
            variants = [r for r in self._snapshot.rows if r.stem == rs]
            if not variants:
                return None
            ref_sb = self._reference_sb()
            if ref_sb:
                match = next(
                    (r for r in variants if r.selbstbeteiligung.strip() == ref_sb), None
                )
                if match is not None:
                    return match
            priced = [r for r in variants if r.monatlich_eur is not None]
            return min(priced, key=lambda r: r.monatlich_eur) if priced else variants[0]

        def _reference_info(self) -> tuple[float | None, str | None]:
            """(monthly premium, SB band) of the reference tariff (current contract)."""
            row = self._reference_row()
            if row is not None and row.monatlich_eur is not None:
                return row.monatlich_eur, row.selbstbeteiligung
            return None, None

        @staticmethod
        def _delta_cell(
            price: float | None, sb: str | None, ref_price: float | None, ref_sb: str | None
        ) -> str:
            """Δ vs the reference premium. Prefixes ≈ when the SB band differs (so the
            comparison is not 1:1) and renders an exact match as a neutral ±0."""
            if ref_price is None or price is None:
                return "[dim]—[/dim]"
            d = price - ref_price
            if abs(d) < 0.005:
                return "[dim]±0[/dim]"
            pct = d / ref_price * 100 if ref_price else 0.0
            color = "bright_green" if d < 0 else "bright_red"
            sign = "" if d < 0 else "+"
            approx = "≈" if (sb or "") != (ref_sb or "") else ""
            return f"[{color}]{approx}{sign}{d:.2f} ({sign}{pct:.0f}%)[/{color}]"

        def _docs_label(self, stem: str) -> str:
            seen: list[str] = []
            for dd in self._doc_index.get(stem, []):
                lbl = _DOCTYPE_SHORT.get(dd.get("doctype", ""), dd.get("doctype", ""))
                if lbl and lbl not in seen:
                    seen.append(lbl)
            return "·".join(seen) if seen else "[dim]—[/dim]"

        def _populate_favorites_table(self) -> None:
            try:
                table: DataTable = self.query_one("#fav-table", DataTable)
            except NoMatches:
                return

            table.clear(columns=True)
            table.add_columns("★", "Insurer", "Product", "Note", "€/mo", "SB", "Δ ref", "Status")
            self._fav_rows = {}

            ref_price, ref_sb = self._reference_info()

            # Banner: knock-out rule + the reference anchor + a drill-in hint.
            try:
                ko = self.query_one("#fav-knockout", Label)
                parts: list[str] = []
                ko_text = self._favorites.get("knockout", "")
                if ko_text:
                    parts.append(f"⊘ {ko_text}")
                ref_row = self._reference_row()
                if ref_row is not None and ref_price is not None:
                    parts.append(
                        f"◆ Referenz: {ref_row.insurer} {ref_row.product} "
                        f"(SB {ref_sb}, €{ref_price:.2f}/mo) — \\[R] setzt eine andere; "
                        f"Δ vergleicht dagegen, ≈ markiert eine abweichende SB-Stufe (nicht 1:1)."
                    )
                parts.append("↵ Zeile wählen → Detail · \\[R] Referenz · \\[u] Favorit")
                parts.append(STATUS_LEGEND)
                ko.update("\n".join(parts))
            except NoMatches:
                pass

            if not self._snapshot:
                return

            # Resolve, then order by Tarifnote then price (best decision first).
            entries: list[tuple[dict, SnapshotRow | None, list[SnapshotRow]]] = []
            for fav in self._favorites.get("favorites", []):
                row, variants = match_favorite(self._snapshot, fav)
                entries.append((fav, row, variants))

            def _sort_key(e: tuple[dict, SnapshotRow | None, list]) -> tuple[float, float]:
                _f, r, _v = e
                if r is None:
                    return (9999.0, 9999.0)
                try:
                    note = float((r.tarifnote or "").replace(",", "."))
                except (ValueError, AttributeError):
                    note = 9999.0
                return (note, r.monatlich_eur if r.monatlich_eur is not None else 9999.0)

            entries.sort(key=_sort_key)

            for idx, (fav, row, variants) in enumerate(entries):
                key = f"fav-{idx}"  # unique per board row, never collides
                self._fav_rows[key] = (row, fav)
                if row is None:
                    table.add_row(
                        "[dim]?[/dim]",
                        fav.get("insurer") or "",
                        fav.get("product") or "",
                        "—", "—", "—", "—",
                        self._docs_label(fav.get("stem", "")),
                        key=key,
                    )
                    continue

                if self._is_reference(fav):
                    star = "[bright_yellow]◆[/bright_yellow]"
                elif fav.get("recommended"):
                    star = "[bright_green]▶[/bright_green]"
                else:
                    star = "[yellow]★[/yellow]"

                nc = _tarifnote_color(row.tarifnote)
                note_col = f"[{nc}]{row.tarifnote}[/{nc}]" if row.tarifnote else "—"
                price_str = f"{row.monatlich_eur:.2f}" if row.monatlich_eur is not None else "—"
                pc = _price_color(row.monatlich_eur, self._q1, self._q3)
                price_col = f"[{pc}]{price_str}[/{pc}]"

                if self._is_reference(fav):
                    delta_col = "[dim]— (Referenz)[/dim]"
                else:
                    delta_col = self._delta_cell(
                        row.monatlich_eur, row.selbstbeteiligung, ref_price, ref_sb
                    )

                sb_cell = row.selbstbeteiligung or "—"
                if len(variants) > 1:
                    sb_cell = f"{sb_cell} [dim]·{len(variants)}▾[/dim]"

                docs_cell = f"{_status_glyph(row)} {self._docs_label(fav.get('stem', ''))}"
                table.add_row(
                    star, row.insurer, row.product, note_col, price_col,
                    sb_cell, delta_col, docs_cell,
                    key=key,
                )

        def _render_favorite_detail(self, row: SnapshotRow, fav: dict) -> str:
            lines: list[str] = []
            lines.append(f"[bold]{row.insurer}[/bold] — [italic]{row.product}[/italic]")
            tag = fav.get("tag", "")
            if tag or self._is_reference(fav):
                if self._is_reference(fav):
                    marker, mcolor = "◆", "bright_yellow"
                elif fav.get("recommended"):
                    marker, mcolor = "▶", "bright_green"
                else:
                    marker, mcolor = "★", "yellow"
                lines.append(f"[{mcolor}]{marker} {tag or 'Referenz'}[/{mcolor}]")
            lines.append("")

            nc = _tarifnote_color(row.tarifnote)
            lines.append(f"Tarifnote : [{nc}]{row.tarifnote or '—'}[/{nc}]")
            price = f"{row.monatlich_eur:.2f}" if row.monatlich_eur is not None else "—"
            lines.append(
                f"€/Monat   : [bright_green]{price}[/bright_green]   "
                f"(SB {row.selbstbeteiligung or '—'})"
            )
            ref_price, ref_sb = self._reference_info()
            if ref_price is not None and row.monatlich_eur is not None and not self._is_reference(fav):
                d = row.monatlich_eur - ref_price
                if abs(d) < 0.005:
                    lines.append("vs. Referenz: [dim]±0 €/mo[/dim]")
                else:
                    pct = d / ref_price * 100 if ref_price else 0.0
                    color = "bright_green" if d < 0 else "bright_red"
                    sign = "" if d < 0 else "+"
                    lines.append(
                        f"vs. Referenz: [{color}]{sign}{d:.2f} €/mo ({sign}{pct:.0f}%)[/{color}]"
                    )
                    if (row.selbstbeteiligung or "") != (ref_sb or ""):
                        lines.append(
                            f"  [yellow]≈ andere SB-Stufe[/yellow] [dim]({row.selbstbeteiligung} "
                            f"vs. Referenz {ref_sb}) — nicht 1:1[/dim]"
                        )
            lines.append("")

            _, variants = match_favorite(self._snapshot, fav)
            if len(variants) > 1:
                lines.append("[underline]SB-Varianten[/underline]")
                for v in sorted(
                    variants, key=lambda r: r.monatlich_eur if r.monatlich_eur is not None else 9999.0
                ):
                    p = f"{v.monatlich_eur:.2f}" if v.monatlich_eur is not None else "—"
                    mark = " [bright_yellow]◀ shown[/bright_yellow]" if v.key == row.key else ""
                    lines.append(f"  {v.selbstbeteiligung:<18} €{p}{mark}")
                lines.append("")

            has_detail = _load_detail(row.insurer, row.product) is not None
            docs = self._doc_index.get(fav.get("stem", ""), [])
            if docs:
                lines.append("[underline]Quelldokumente (URLs gesichert)[/underline]")
                for dd in docs:
                    lbl = _DOCTYPE_SHORT.get(dd.get("doctype", ""), dd.get("doctype", ""))
                    fname = (dd.get("file") or "")[:54]
                    lines.append(f"  [cyan]{lbl:<6}[/cyan] {fname}")
                if not has_detail:
                    lines.append(
                        "[bright_yellow]  \\[g] herunterladen + analysieren[/bright_yellow]"
                    )
                    lines.append(
                        f"  [dim]→ fetch_docs.py {fav.get('stem')} --into-raw"
                        " → ingest → extract[/dim]"
                    )
                lines.append("")

            if has_detail:
                lines.append(
                    "[bright_green]✓ Detail-Datensatz eingelesen — Module oben.[/bright_green]"
                )
            else:
                lines.append(
                    "[dim italic]Noch keine AVB/PIB eingelesen — \\[g] lädt + analysiert "
                    "sie für den Modul-Vergleich.[/dim italic]"
                )
            return "\n".join(lines)

        # --- Market table ---

        def _visible_rows(self) -> list[SnapshotRow]:
            if not self._snapshot:
                return []
            f = self.filter_text.lower()
            rows = [
                r
                for r in self._snapshot.rows
                if not f or f in r.insurer.lower() or f in r.product.lower()
            ]

            key = self.sort_col
            if key == "position":
                rows.sort(key=lambda r: r.position, reverse=not self.sort_asc)
            elif key == "insurer":
                rows.sort(key=lambda r: r.insurer.lower(), reverse=not self.sort_asc)
            elif key == "note":
                def note_key(r: SnapshotRow) -> float:
                    try:
                        return float(r.tarifnote.replace(",", "."))
                    except (ValueError, AttributeError):
                        return 9999.0
                rows.sort(key=note_key, reverse=not self.sort_asc)
            elif key == "price":
                rows.sort(
                    key=lambda r: r.monatlich_eur if r.monatlich_eur is not None else 9999.0,
                    reverse=not self.sort_asc,
                )
            return rows

        def _populate_market_table(self) -> None:
            try:
                table: DataTable = self.query_one("#market-table", DataTable)
            except NoMatches:
                return

            table.clear(columns=True)
            table.add_columns("#", "St", "Insurer", "Product", "Note", "€/mo", "SB")

            rows = self._visible_rows()
            row_count_label = self.query_one("#filter-input", Input)
            # update placeholder with count
            row_count_label.placeholder = (
                f"Filter by insurer or product… ({len(rows)} shown)"
            )
            try:
                date = self._snapshot.date if self._snapshot else "?"
                self.query_one("#market-legend", Label).update(
                    f"{STATUS_LEGEND}   [dim]· gelesen am {date}[/dim]"
                )
            except NoMatches:
                pass

            for r in rows:
                star = _status_glyph(r)

                note_col = (
                    f"[{_tarifnote_color(r.tarifnote)}]{r.tarifnote}[/{_tarifnote_color(r.tarifnote)}]"
                    if r.tarifnote
                    else "—"
                )

                price_str = f"{r.monatlich_eur:.2f}" if r.monatlich_eur is not None else "—"
                price_col = (
                    f"[{_price_color(r.monatlich_eur, self._q1, self._q3)}]{price_str}[/{_price_color(r.monatlich_eur, self._q1, self._q3)}]"
                )

                table.add_row(
                    str(r.position),
                    star,
                    r.insurer,
                    r.product,
                    note_col,
                    price_col,
                    r.selbstbeteiligung or "—",
                    key=r.key or f"{r.position}",
                )

            # update diff tab while we're refreshing
            self._populate_diff()

        # --- Detail panel (sidebar) ---

        def _doc_entry(self, row: SnapshotRow) -> dict | None:
            """Resolve a snapshot row to its harvested manifest entry (stem + docs)
            via the row's canonical stem (computed once at load via the same manifest
            join). None when the tariff has no harvested source URLs."""
            if row.stem:
                return self._doc_by_stem.get(row.stem)
            return None

        def _render_docs_block(
            self, row: SnapshotRow, detail: DetailRecord | None
        ) -> str:
            """The harvested source-document list + the on-demand [g] pull hint.
            Shared by the Market and Favorites detail bands. Returns "" if there is
            nothing to say (a tariff with no harvested URLs that is already
            analyzed)."""
            entry = self._doc_entry(row)
            lines: list[str] = []
            if entry and entry.get("docs"):
                lines.append("[underline]Quelldokumente[/underline]")
                for dd in entry["docs"]:
                    lbl = _DOCTYPE_SHORT.get(dd.get("doctype", ""), dd.get("doctype", ""))
                    fname = (dd.get("file") or "")[:60]
                    lines.append(f"  [cyan]{lbl:<6}[/cyan] {fname}")
                if detail:
                    lines.append("[bright_green]  ✓ analysiert[/bright_green]")
                else:
                    lines.append(
                        "[bright_yellow]  \\[g] herunterladen + analysieren[/bright_yellow]"
                    )
                    lines.append(
                        f"  [dim]→ fetch_docs.py {entry.get('stem')} --into-raw"
                        " → ingest → extract[/dim]"
                    )
            elif not detail:
                lines.append(
                    "[dim]Quell-PDFs noch nicht geharvestet — Browser-Schritt"
                    " (Tarifdetails öffnen) nötig.[/dim]"
                )
            return "\n".join(lines)

        def _render_market_detail(self, row: SnapshotRow) -> str:
            """Full tariff detail (modules, coverage, premium, benefits, exclusions)
            plus the source-document / [g] block, for the inline Market band."""
            detail = _load_detail(row.insurer, row.product)
            parts = [self._render_detail_full(row)]
            docs = self._render_docs_block(row, detail)
            if docs:
                parts.append(docs)
            return "\n\n".join(parts)

        # --- Full detail tab ---

        def _render_detail_full(self, row: SnapshotRow) -> str:
            detail = _load_detail(row.insurer, row.product)
            if not detail:
                return (
                    f"[bold]{row.insurer}[/bold] — {row.product}\n\n"
                    "[dim italic]No detailed record ingested yet.[/dim italic]\n"
                    "[dim]Run: uv run scripts/ingest.py  to extract tariff details.[/dim]"
                )

            lines: list[str] = []
            badge = (
                "[bright_green]enriched record[/bright_green]"
                if detail.is_enriched
                else "[cyan]base tariff record[/cyan]"
            )
            lines.append(f"[bold underline]{detail.insurer}[/bold underline]  — {badge}")
            lines.append(f"[bold]{detail.tariff}[/bold]")
            if detail.stand:
                lines.append(f"Stand: {detail.stand}")
            lines.append("")

            # Modules
            lines.append("[bold underline]Modules[/bold underline]")
            for mod_key, label in MODULE_LABELS.items():
                mod = detail.modules.get(mod_key, {})
                included = mod.get("included", False)
                badge_str = _module_badge(mod)
                note_str = mod.get("note") or ""
                lines.append(f"  {label:<22} {badge_str}")
                if included and note_str:
                    lines.append(f"    [dim]{note_str}[/dim]")
            lines.append("")

            # Coverage
            cov = detail.coverage
            if cov:
                lines.append("[bold underline]Coverage[/bold underline]")
                if cov.get("versicherungssumme"):
                    lines.append(f"  Versicherungssumme:  {cov['versicherungssumme']}")
                if cov.get("selbstbeteiligung"):
                    lines.append(f"  Selbstbeteiligung:   {cov['selbstbeteiligung']}")
                if cov.get("wartezeit_monate") is not None:
                    lines.append(f"  Wartezeit:           {cov['wartezeit_monate']} Monate")
                if cov.get("wartezeit_ausnahmen"):
                    lines.append("  Wartezeit-Ausnahmen:")
                    for ex in cov["wartezeit_ausnahmen"]:
                        lines.append(f"    • {ex}")
                if cov.get("geltungsbereich"):
                    lines.append(f"  Geltungsbereich:     {cov['geltungsbereich']}")
                if cov.get("vertragslaufzeit"):
                    lines.append(f"  Vertragslaufzeit:    {cov['vertragslaufzeit']}")
                lines.append("")

            # Premium
            if detail.beitrag:
                lines.append("[bold underline]Premium[/bold underline]")
                m = detail.beitrag.get("monatlich_eur")
                y = detail.beitrag.get("jaehrlich_eur")
                if m is not None:
                    lines.append(f"  [bright_green]€ {m:.2f} / month[/bright_green]")
                if y is not None:
                    lines.append(f"  € {y:.2f} / year")
                if detail.beitrag.get("quelle"):
                    lines.append(f"  Source: {detail.beitrag['quelle']}")
                lines.append("")

            # Leistungen
            if detail.leistungen:
                lines.append("[bold underline]Leistungen (benefits)[/bold underline]")
                for item in detail.leistungen:
                    lines.append(f"  [green]✓[/green] {item}")
                lines.append("")

            # Ausschlüsse
            if detail.ausschluesse:
                lines.append("[bold underline]Ausschlüsse (exclusions)[/bold underline]")
                for item in detail.ausschluesse:
                    lines.append(f"  [red]✗[/red] {item}")
                lines.append("")

            # Besonderheiten
            if detail.besonderheiten:
                lines.append("[bold underline]Besonderheiten (highlights)[/bold underline]")
                for item in detail.besonderheiten:
                    lines.append(f"  [yellow]★[/yellow] {item}")

            return "\n".join(lines)

        # --- Diff tab ---

        def _populate_diff(self) -> None:
            try:
                diff_widget: Static = self.query_one("#diff-content", Static)
            except NoMatches:
                return

            if len(self._all_snapshots) < 2:
                diff_widget.update(
                    "[dim italic]Only one snapshot available — nothing to diff.\n"
                    "Run the snapshot pipeline again on a different day to see price changes.[/dim italic]"
                )
                return

            # Compare oldest vs. newest
            _, old_path = self._all_snapshots[0]
            _, new_path = self._all_snapshots[-1]
            old_snap = load_snapshot(old_path)
            new_snap = load_snapshot(new_path)

            if old_snap is None or new_snap is None:
                diff_widget.update("[red]Failed to load snapshots for diff.[/red]")
                return

            changes, added, removed = _compute_diff(old_snap, new_snap)

            lines: list[str] = []
            lines.append(
                f"[bold]Snapshot diff:[/bold]  "
                f"[dim]{old_snap.date}[/dim] → [bold]{new_snap.date}[/bold]"
            )
            lines.append("")

            if changes:
                lines.append(f"[underline]Price changes ({len(changes)})[/underline]")
                for key, old_p, new_p, delta in sorted(changes, key=lambda x: x[3]):
                    sign = "+" if delta > 0 else ""
                    color = "bright_red" if delta > 0 else "bright_green"
                    lines.append(
                        f"  {key[:50]:<50}  "
                        f"{old_p:.2f} → {new_p:.2f}  "
                        f"[{color}]{sign}{delta:.2f}[/{color}]"
                    )
                lines.append("")

            if added:
                lines.append(f"[underline bright_green]New tariffs ({len(added)})[/underline bright_green]")
                for k in added:
                    lines.append(f"  [bright_green]+[/bright_green] {k}")
                lines.append("")

            if removed:
                lines.append(f"[underline bright_red]Removed tariffs ({len(removed)})[/underline bright_red]")
                for k in removed:
                    lines.append(f"  [bright_red]−[/bright_red] {k}")
                lines.append("")

            if not changes and not added and not removed:
                lines.append("[dim italic]No changes detected between snapshots.[/dim italic]")

            diff_widget.update("\n".join(lines))

        # --- Event handlers ---

        @on(DataTable.RowHighlighted, "#market-table")
        def on_market_highlighted(self, event: DataTable.RowHighlighted) -> None:
            """A single click / arrow move highlights a row — track it as the active
            target and refresh the (possibly hidden) detail band live."""
            key = str(event.row_key.value) if event.row_key.value is not None else None
            self.selected_row_key = key
            if not self._snapshot or key is None:
                return
            row = next(
                (r for r in self._snapshot.rows if (r.key or str(r.position)) == key), None
            )
            if row is None:
                return
            self._active_row = row  # target for the [g] download/analyze action
            self._active_fav = None
            self._refresh_market_detail()

        @on(DataTable.RowHighlighted, "#fav-table")
        def on_fav_highlighted(self, event: DataTable.RowHighlighted) -> None:
            key = str(event.row_key.value) if event.row_key.value is not None else None
            if not key:
                return
            entry = self._fav_rows.get(key)
            if entry is None:
                return
            row, fav = entry
            self._active_row = row  # may be None (favorite not in snapshot)
            self._active_fav = fav
            self._refresh_fav_detail()

        # Enter / click-on-highlighted: open the detail band on the row.
        @on(DataTable.RowSelected, "#market-table")
        def on_market_selected(self, event: DataTable.RowSelected) -> None:
            self.on_market_highlighted(event)  # ensure active row is current
            self._show_detail()

        @on(DataTable.RowSelected, "#fav-table")
        def on_fav_selected(self, event: DataTable.RowSelected) -> None:
            self.on_fav_highlighted(event)
            self._show_detail()

        @on(DataTable.HeaderSelected, "#market-table")
        def on_header_selected(self, event: DataTable.HeaderSelected) -> None:
            col_map = {0: "position", 2: "insurer", 4: "note", 5: "price"}
            col = col_map.get(event.column_index)
            if col is None:
                return
            if self.sort_col == col:
                self.sort_asc = not self.sort_asc
            else:
                self.sort_col = col
                self.sort_asc = True
            self._populate_market_table()

        @on(Input.Changed, "#filter-input")
        def on_filter_changed(self, event: Input.Changed) -> None:
            self.filter_text = event.value
            self._populate_market_table()

        # --- Actions ---

        def action_focus_filter(self) -> None:
            try:
                self.query_one("#filter-input", Input).focus()
            except NoMatches:
                pass

        def action_clear_filter(self) -> None:
            try:
                inp = self.query_one("#filter-input", Input)
                inp.value = ""
                self.filter_text = ""
                self.query_one("#market-table", DataTable).focus()
            except NoMatches:
                pass

        def action_sort_price(self) -> None:
            self.sort_col = "price"
            self.sort_asc = True
            self._populate_market_table()

        def action_sort_note(self) -> None:
            self.sort_col = "note"
            self.sort_asc = True
            self._populate_market_table()

        def action_sort_position(self) -> None:
            self.sort_col = "position"
            self.sort_asc = True
            self._populate_market_table()

        def action_switch_tab(self, tab_id: str) -> None:
            try:
                tabs = self.query_one("#tabs", TabbedContent)
                tabs.active = tab_id
            except NoMatches:
                pass

        def _reload_all(self) -> None:
            """Reload every data source from disk and repaint both tables, the header
            and whichever detail band is shown. Used after [g], a favorite edit or a
            delete so the UI reflects the new on-disk state."""
            self._load_data()
            self._populate_favorites_table()
            self._populate_market_table()
            self._update_header()
            self._refresh_market_detail()
            self._refresh_fav_detail()

        def action_refresh_data(self) -> None:
            self._reload_all()

        # --- Build the CHECK24 result URL ([b]) ---

        def action_build_query(self) -> None:
            """Rebuild the CHECK24 result URL(s) from the saved profile and write them
            to tmp/ for the manual browser + scrape workflow (no headless path — bot
            gating). Reuses scripts/check24_query.py for the lever decode."""
            import contextlib
            import importlib.util
            import io
            from urllib.parse import parse_qsl, urlencode

            qpath = REPO_ROOT / "scripts" / "check24_query.py"
            try:
                spec = importlib.util.spec_from_file_location("check24_query", qpath)
                cq = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(cq)
            except Exception as exc:  # noqa: BLE001 — surface any load failure to the user
                self.notify(f"check24_query.py nicht ladbar: {exc}", severity="error", timeout=6)
                return

            ppath = REPO_ROOT / "config" / "check24-profile.json"
            epath = REPO_ROOT / "config" / "check24-profile.example.json"
            is_example = False
            if ppath.is_file():
                profile = json.loads(ppath.read_text())
            elif epath.is_file():
                profile = json.loads(epath.read_text())
                is_example = True
            else:
                self.notify(
                    "Kein Query-Profil (config/check24-profile.json).",
                    severity="error",
                    timeout=6,
                )
                return

            base = profile.get("base_url")
            query = profile.get("query")
            if not base or query is None:
                self.notify("Profil ohne base_url/query.", severity="error", timeout=6)
                return

            pairs = parse_qsl(query, keep_blank_values=True)
            saved_url = base + "?" + urlencode(pairs)
            all_url = base + "?" + urlencode([(k, v) for k, v in pairs if k not in cq.PIN_KEYS])

            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    cq.show(pairs)
                levers = buf.getvalue().strip()
            except Exception:  # noqa: BLE001 — the decode is best-effort context only
                levers = ""

            out = REPO_ROOT / "tmp" / "check24-query.txt"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                f"# saved query\n{saved_url}\n\n# all insurers (provider/package pins dropped)\n"
                f"{all_url}\n",
                encoding="utf-8",
            )
            self.push_screen(QueryUrlScreen(levers, str(out.relative_to(REPO_ROOT)), is_example))

        # --- Favorites management ([u] toggle, [D] delete) ---

        def _save_favorites(self) -> None:
            """Persist the (tracked, PII-free) shortlist back to config/favorites.json."""
            path = REPO_ROOT / "config" / "favorites.json"
            path.write_text(
                json.dumps(self._favorites, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

        def _is_favorite_stem(self, stem: str | None) -> bool:
            if not stem:
                return False
            return any(f.get("stem") == stem for f in self._favorites.get("favorites", []))

        def _active_identity(self) -> tuple[str, str, str | None] | None:
            """(insurer, product, stem) of the active favorite or market row, or None."""
            if self._active_fav is not None:
                f = self._active_fav
                return f.get("insurer", ""), f.get("product", ""), f.get("stem")
            if self._active_row is not None:
                r = self._active_row
                return r.insurer, r.product, r.stem
            return None

        def action_toggle_favorite(self) -> None:
            """Add the active market row to the shortlist, or remove the active
            favorite / an already-favorited row from it."""
            ident = self._active_identity()
            if ident is None:
                self.notify("Erst eine Zeile wählen (Pfeile / Klick).", severity="warning")
                return
            insurer, product, stem = ident
            favs = self._favorites.setdefault("favorites", [])

            def matches(f: dict) -> bool:
                if stem and f.get("stem"):
                    return f.get("stem") == stem
                return f.get("insurer") == insurer and f.get("product") == product

            if any(matches(f) for f in favs):
                self._favorites["favorites"] = [f for f in favs if not matches(f)]
                self._save_favorites()
                self.notify(f"Aus Favoriten entfernt: {insurer} {product}", timeout=4)
            else:
                entry: dict[str, Any] = {"insurer": insurer, "product": product}
                if stem:
                    entry["stem"] = stem
                if self._active_row is not None and self._active_row.selbstbeteiligung:
                    entry["show_sb"] = self._active_row.selbstbeteiligung
                entry["tag"] = "in TUI hinzugefügt"
                favs.append(entry)
                self._save_favorites()
                self.notify(f"Zu Favoriten hinzugefügt: {insurer} {product}", timeout=4)
            self._reload_all()

        def action_set_reference(self) -> None:
            """Make the active row the comparison baseline; every Δ recomputes against
            it. Writes reference_stem + reference_sb to config/favorites.json."""
            ident = self._active_identity()
            if ident is None:
                self.notify("Erst eine Zeile wählen (Pfeile / Klick).", severity="warning")
                return
            insurer, product, stem = ident
            if not stem:
                self.notify(
                    "Referenz braucht einen kanonischen stem (Tarif ohne Manifest-Eintrag).",
                    severity="warning",
                    timeout=6,
                )
                return
            sb = self._active_row.selbstbeteiligung if self._active_row else ""
            if not sb and self._active_fav:
                sb = self._active_fav.get("show_sb", "")
            self._favorites["reference_stem"] = stem
            self._favorites["reference_sb"] = sb
            # Retire the legacy per-favorite flag — reference_stem is now canonical.
            for f in self._favorites.get("favorites", []):
                f.pop("reference", None)
            self._save_favorites()
            self.notify(
                f"Referenz: {insurer} {product} (SB {sb or '—'}) — Δ neu berechnet.",
                timeout=5,
            )
            self._reload_all()

        def action_delete_data(self) -> None:
            """Delete a tariff's locally stored data, with a scope chosen in a modal."""
            ident = self._active_identity()
            if ident is None:
                self.notify("Erst eine Zeile wählen (Pfeile / Klick).", severity="warning")
                return
            insurer, product, stem = ident
            if not stem:
                self.notify(
                    "Kein kanonischer stem für diese Zeile — nichts lokal gespeichert.",
                    severity="warning",
                    timeout=6,
                )
                return
            label = f"{insurer} {product}"
            is_fav = self._is_favorite_stem(stem)

            def _go(scope: str | None) -> None:
                if scope:
                    self._do_delete(stem, scope, label)

            self.push_screen(DeleteDataScreen(stem, label, is_fav), _go)

        def _prune_ingest_manifest(self, insurer_part: str, tariff_part: str) -> None:
            """Drop a tariff's documents from data/extracted/manifest.json so a future
            extract run does not resurrect a deleted tariff from a dangling entry."""
            mp = REPO_ROOT / "data" / "extracted" / "manifest.json"
            if not mp.is_file():
                return
            try:
                m = json.loads(mp.read_text())
            except (json.JSONDecodeError, OSError):
                return
            docs = m.get("documents", [])
            kept = [
                d for d in docs
                if not (d.get("insurer") == insurer_part and d.get("tariff") == tariff_part)
            ]
            if len(kept) != len(docs):
                m["documents"] = kept
                mp.write_text(json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8")

        def _do_delete(self, stem: str, scope: str, label: str) -> None:
            import shutil

            removed: list[str] = []
            for sub in ("tariffs", "enriched"):
                p = REPO_ROOT / "out" / sub / f"{stem}.json"
                if p.exists():
                    p.unlink()
                    removed.append(f"out/{sub}/{stem}.json")

            if scope in ("purge", "purge_unfav"):
                insurer_part, _, tariff_part = stem.partition("__")
                for base in ("raw", "extracted"):
                    d = REPO_ROOT / "data" / base / insurer_part / tariff_part
                    if d.is_dir():
                        shutil.rmtree(d)
                        removed.append(f"data/{base}/{insurer_part}/{tariff_part}/")
                self._prune_ingest_manifest(insurer_part, tariff_part)

            if scope == "purge_unfav":
                favs = self._favorites.get("favorites", [])
                pruned = [f for f in favs if f.get("stem") != stem]
                if len(pruned) != len(favs):
                    self._favorites["favorites"] = pruned
                    self._save_favorites()
                    removed.append("config/favorites.json (unfavorite)")

            if removed:
                self.notify(f"Gelöscht ({label}): " + ", ".join(removed), timeout=8)
            else:
                self.notify(f"Nichts zu löschen für {label}.", severity="information")
            self._reload_all()

        # --- Inline detail band (toggled with [d]) ---

        def _detail_band_for_tab(self) -> tuple[str, str] | None:
            """(#band-container, #content-static) for the active tab, or None when
            the active tab has no detail band (e.g. Diff)."""
            try:
                active = self.query_one("#tabs", TabbedContent).active
            except NoMatches:
                return None
            if active == "favorites":
                return "#fav-detail", "#fav-detail-content"
            if active == "market":
                return "#detail-panel", "#detail-content"
            return None

        def _render_active_into(self, ids: tuple[str, str]) -> None:
            """Render the current active row/favorite into a band's Static."""
            band_id, content_id = ids
            try:
                content = self.query_one(content_id, Static)
            except NoMatches:
                return
            if band_id == "#fav-detail":
                fav, row = self._active_fav, self._active_row
                if fav is None:
                    content.update("[dim]Favoriten-Zeile wählen (Pfeile / Klick).[/dim]")
                elif row is None:
                    content.update(
                        f"[bold]{fav.get('insurer', '')}[/bold] — "
                        f"[italic]{fav.get('product', '')}[/italic]\n\n"
                        "[yellow]Kein passender Tarif im aktuellen Snapshot.[/yellow]\n"
                        "[dim]Liste oder Snapshot ist veraltet — config/favorites.json "
                        "oder scripts/snapshot.py auffrischen.[/dim]"
                    )
                else:
                    content.update(self._render_favorite_detail(row, fav))
            else:  # market band
                row = self._active_row
                if row is None:
                    content.update("[dim]Markt-Zeile wählen (Pfeile / Klick).[/dim]")
                else:
                    content.update(self._render_market_detail(row))

        def action_toggle_detail(self) -> None:
            """Show/hide the inline detail band below the active tab's table."""
            ids = self._detail_band_for_tab()
            if ids is None:
                self.notify("Kein Detail-Panel auf diesem Tab.", severity="information")
                return
            band_id, _ = ids
            try:
                band = self.query_one(band_id)
            except NoMatches:
                return
            band.display = not band.display
            if band.display:
                self._render_active_into(ids)
                band.scroll_home(animate=False)

        def _show_detail(self) -> None:
            """Reveal the active tab's detail band and render the active row (used by
            Enter / click-on-highlighted)."""
            ids = self._detail_band_for_tab()
            if ids is None:
                return
            band_id, _ = ids
            try:
                band = self.query_one(band_id)
            except NoMatches:
                return
            band.display = True
            self._render_active_into(ids)

        def _refresh_market_detail(self) -> None:
            """Re-render the Market band live — only when it is currently shown."""
            try:
                band = self.query_one("#detail-panel")
            except NoMatches:
                return
            if band.display:
                self._render_active_into(("#detail-panel", "#detail-content"))

        def _refresh_fav_detail(self) -> None:
            try:
                band = self.query_one("#fav-detail")
            except NoMatches:
                return
            if band.display:
                self._render_active_into(("#fav-detail", "#fav-detail-content"))

        # --- On-demand: download + analyze the selected tariff ([g]) ---

        def action_fetch_docs(self) -> None:
            """Resolve the selected row to its harvested source PDFs and, after a
            confirm, download + run the analyze pipeline in the background."""
            row = self._active_row
            if row is None:
                self.notify("Erst eine Zeile wählen (↵).", severity="warning")
                return
            entry = self._doc_entry(row)
            if not entry or not entry.get("docs"):
                self.notify(
                    "Keine geharvesteten Quell-URLs — erst Browser-Schritt "
                    "(Tarifdetails öffnen).",
                    severity="warning",
                    timeout=6,
                )
                return
            if _load_detail(row.insurer, row.product):
                self.notify(
                    "Schon analysiert — [d] zeigt die Details.", severity="information"
                )
                return

            def _go(confirmed: bool | None) -> None:
                if confirmed:
                    self._run_pipeline(entry, row)

            self.push_screen(ConfirmFetchScreen(entry, ANALYZE_MODEL), _go)

        @work(thread=True, exclusive=True, group="pipeline")
        def _run_pipeline(self, entry: dict, row: SnapshotRow) -> None:
            """Run fetch_docs --apply → intake → ingest → extract for one tariff.
            Runs off the UI thread; status is posted back via call_from_thread."""
            import subprocess

            stem = entry.get("stem", "")
            # Download straight into the canonical data/raw/<stem>/ layout (--into-raw),
            # so ingest/extract name the record exactly <stem>.json — no filename-guessing
            # intake step that could misname it and hide the result from the TUI.
            steps = [
                ("Download", ["uv", "run", "scripts/fetch_docs.py", stem, "--into-raw"]),
                ("Ingest", ["uv", "run", "scripts/ingest.py"]),
                ("Extract", ["uv", "run", "scripts/extract.py", "--model", ANALYZE_MODEL]),
            ]
            self.call_from_thread(
                self.notify, f"Pipeline gestartet: {stem} …", timeout=4
            )
            for name, cmd in steps:
                try:
                    proc = subprocess.run(
                        cmd,
                        cwd=str(REPO_ROOT),
                        capture_output=True,
                        text=True,
                        timeout=600,
                    )
                except (subprocess.TimeoutExpired, OSError) as exc:
                    self.call_from_thread(
                        self.notify,
                        f"{name} fehlgeschlagen: {exc}",
                        severity="error",
                        timeout=8,
                    )
                    return
                if proc.returncode != 0:
                    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
                    tail = detail[-1] if detail else f"exit {proc.returncode}"
                    self.call_from_thread(
                        self.notify,
                        f"{name} fehlgeschlagen: {tail}",
                        severity="error",
                        timeout=10,
                    )
                    return
                self.call_from_thread(self.notify, f"{name} ✓", timeout=2)
            self.call_from_thread(self._after_pipeline, row)

        def _after_pipeline(self, row: SnapshotRow) -> None:
            """Reload data so the freshly extracted record shows, then refresh panels."""
            self._load_data()
            self._populate_market_table()
            self._populate_favorites_table()
            # Re-render whichever detail band is currently shown.
            self._refresh_market_detail()
            self._refresh_fav_detail()
            self.notify(
                f"Analyse fertig: {row.insurer} {row.product} — [d] zeigt die Details.",
                timeout=8,
            )

    app = CheckApp(snapshot_path=snapshot_path)

    if screenshot_dir is not None:
        import asyncio

        async def _shoot() -> None:
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            async with app.run_test(size=(140, 48)) as pilot:
                await pilot.pause()
                tabs = app.query_one("#tabs", TabbedContent)

                # --- Favorites: reveal the detail band on the first favorite ---
                tabs.active = "favorites"
                await pilot.pause()
                first_fav = None
                for _k, (row, fav) in app._fav_rows.items():
                    if row is not None:
                        first_fav = (row, fav)
                        break
                if first_fav is not None:
                    app._active_row, app._active_fav = first_fav
                    try:
                        app.query_one("#fav-table", DataTable).move_cursor(row=0)
                    except Exception:
                        pass
                    app._show_detail()
                await pilot.pause()
                app.save_screenshot(filename="favorites.svg", path=str(screenshot_dir))

                # --- Market: first the default (collapsed) view — full-width table,
                # no band — then reveal it with the [g] affordance visible. ---
                if app._snapshot and app._snapshot.rows:
                    market_row = next(
                        (
                            r
                            for r in app._snapshot.rows
                            if app._doc_entry(r) is not None
                            and not _load_detail(r.insurer, r.product)
                        ),
                        app._snapshot.rows[0],
                    )
                    app._active_row, app._active_fav = market_row, None
                tabs.active = "market"
                await pilot.pause()
                app.save_screenshot(filename="market.svg", path=str(screenshot_dir))
                app._show_detail()
                await pilot.pause()
                app.save_screenshot(filename="market-detail.svg", path=str(screenshot_dir))

                # --- Diff ---
                tabs.active = "diff"
                await pilot.pause()
                app.save_screenshot(filename="diff.svg", path=str(screenshot_dir))

                # --- Confirm modal ([g] gate) over the market tab ---
                tabs.active = "market"
                await pilot.pause()
                sample = next(
                    (
                        app._doc_entry(r)
                        for r in app._snapshot.rows
                        if app._doc_entry(r) is not None
                    ),
                    None,
                ) if app._snapshot else None
                if sample is not None:
                    await app.push_screen(ConfirmFetchScreen(sample, ANALYZE_MODEL))
                    await pilot.pause()
                    app.save_screenshot(filename="confirm.svg", path=str(screenshot_dir))

        asyncio.run(_shoot())
        print(
            "Saved screenshots (favorites/market/market-detail/diff/confirm .svg) to "
            f"{screenshot_dir}"
        )
        return

    app.run()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tui.py",
        description=(
            "check0r3000 — Rechtsschutz-Vergleich\n"
            "Interactive terminal UI for browsing German legal-protection-insurance tariffs.\n\n"
            "Run without flags to launch the interactive TUI.\n"
            "Use --selftest to verify data loading without launching the UI."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--snapshot",
        metavar="PATH",
        type=Path,
        default=None,
        help="Path to a specific snapshot JSON (default: most-recent in data/snapshots/)",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="Load data files, print a summary, and exit 0 without launching the UI.",
    )
    parser.add_argument(
        "--screenshot",
        metavar="DIR",
        type=Path,
        default=None,
        help="Render each tab to SVG in DIR (headless), then exit without an interactive UI.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.selftest:
        sys.exit(run_selftest(args.snapshot))
    elif args.screenshot is not None:
        _launch_app(args.snapshot, screenshot_dir=args.screenshot)
    else:
        _launch_app(args.snapshot)


if __name__ == "__main__":
    main()
