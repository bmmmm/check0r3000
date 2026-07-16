"""check0r3000 — the Textual application.

Holds CheckApp (the App subclass driving every tab/binding) plus its private
color/markup helpers. Imports the data layer (tui_data), the formatters
(tui_format) and the modal screens (tui_screens) as flat siblings. CheckApp's
CSS_PATH stays __file__-anchored, so tui.tcss is still found next to this
module in scripts/."""

from __future__ import annotations

import datetime
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# coverage_taxonomy / tui_data / tui_format live alongside this script;
# make scripts/ importable whether this module is reached as a file or via
# `uv run`, then import the siblings.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import coverage_taxonomy as ctax  # noqa: E402
import _providers  # noqa: E402
import scorecard  # noqa: E402  — shared benchmark scoring (eval.py + Benchmark tab)
import magic  # noqa: E402  — Magic Find quality-scoring core (rank / prescore)
import tui_anim  # noqa: E402  — boot-splash frames + pipeline loader bar
from _jsonio import atomic_write_json, load_json_or  # noqa: E402  — shared atomic JSON IO
from _modules import MODULE_LABELS  # noqa: E402  — single source of truth for Baustein labels

from tui_data import (  # noqa: E402
    ChangeInfo,
    DetailRecord,
    REPO_ROOT,
    Snapshot,
    SnapshotRow,
    _DOCTYPE_SHORT,
    _find_latest_snapshot,
    _load_detail,
    _raw_dir_for_stem,
    dominant_provenance,
    external_market_notes,
    external_ratings_for,
    load_all_details,
    load_all_snapshots,
    load_change_summary,
    load_doc_by_tariff,
    load_doc_index,
    load_external_ratings,
    load_favorite_notes,
    load_favorites,
    load_feature_diff,
    load_market_stats,
    load_snapshot,
    match_favorite,
    reset_doc_cache,
    resolve_stem,
    stream_subprocess,
)
from tui_format import (  # noqa: E402
    STATUS_LEGEND,
    VERGLEICH_LABEL_W,
    benchmark_markup,
    external_badge_cell,
    external_rating_lines,
    link_url,
    magic_bar,
    magic_score_cell,
    price_delta,
    record_body_lines,
    sparkline,
    verlauf_row_cells,
    _bewertung_cell,
    _bewertung_color,
    _col_label,
    _esc,
    _level_direction,
    _module_cell,
    _pad_cell,
    _pad_label,
    _price_quartiles,
    _short_geltungsbereich,
    _short_selbstbeteiligung,
    _short_versicherungssumme,
    _short_vertragslaufzeit,
    _short_wartezeit,
    _status_glyph,
    _trunc,
    _vergleich_col_w,
)

from rich.errors import MarkupError as RichMarkupError  # noqa: E402
from rich.text import Text as RichText  # noqa: E402

from textual import on, work  # noqa: E402
from textual.app import App, ComposeResult  # noqa: E402
from textual.binding import Binding  # noqa: E402
from textual.content import Content  # noqa: E402
from textual.markup import MarkupError  # noqa: E402
from textual.containers import Container, ScrollableContainer, Vertical  # noqa: E402
from textual.css.query import NoMatches  # noqa: E402
from textual.timer import Timer  # noqa: E402
from textual.reactive import reactive  # noqa: E402
from textual.widgets import (  # noqa: E402
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)
from textual.widgets.data_table import CellDoesNotExist, RowDoesNotExist  # noqa: E402

from tui_screens import (  # noqa: E402
    CompareManagerScreen,
    CompareTextScreen,
    ConfirmFetchScreen,
    DeleteDataScreen,
    HelpScreen,
    MagicScanScreen,
    NeedsEditorScreen,
    NoteEditScreen,
    OpenSourceScreen,
    QueryEditScreen,
    QuerySaveConfirmScreen,
    QueryUrlScreen,
    SplashScreen,
    UpdateAllScreen,
)

# Model spec for the [g] "download + analyze" pipeline's extract stage. Matches
# extract.py's own default ("claude" = the claude CLI); override without editing
# code via CHECK0R_ANALYZE_MODEL (e.g. a local mlx:/ollama: spec).
ANALYZE_MODEL = os.environ.get("CHECK0R_ANALYZE_MODEL", "claude")

# ---------------------------------------------------------------------------
# Markup containment choke point
# ---------------------------------------------------------------------------
# Per-site _esc at every producer stays the correctness mechanism (display
# fidelity — a deliberate 2026-06-26 bughunt decision). These guards are the
# containment layer behind it: every data-carrying render sink validates its
# markup with the SAME parser the sink itself uses, and a string that would
# raise MarkupError renders escaped-plain instead of crashing the app. A missed
# _esc at a future producer site thus degrades visibly instead of killing the
# TUI mid-session. DataTable parses cells LAZILY (the crash would surface in
# the idle loop, far from the add_row call) with RICH's parser, while Static/
# Label parse with Textual's Content parser — hence two guards.
#
# MARKUP_FALLBACKS records every catch; the hostile-data pilot suite asserts it
# stays EMPTY while driving hostile strings through all producers, so a missed
# _esc still fails the test suite loudly even though it no longer crashes.

MARKUP_FALLBACKS: list[str] = []


def _escaped_plain(markup: str) -> str:
    """Render-safe fallback: every '[' escaped — shows raw tags, never crashes."""
    return markup.replace("[", "\\[")


def guard_content(markup):
    """Validate Static/Label markup (Textual Content parser); non-str passes."""
    if not isinstance(markup, str) or "[" not in markup:
        return markup
    try:
        Content.from_markup(markup)
        return markup
    except MarkupError:
        MARKUP_FALLBACKS.append(markup[:200])
        return _escaped_plain(markup)


def guard_cell(cell):
    """Validate one DataTable cell (Rich parser — what DataTable renders with)."""
    if not isinstance(cell, str) or "[" not in cell:
        return cell
    try:
        RichText.from_markup(cell)
        return cell
    except RichMarkupError:
        MARKUP_FALLBACKS.append(cell[:200])
        return _escaped_plain(cell)


def guard_cells(cells):
    """Guard every cell of one table row."""
    return tuple(guard_cell(c) for c in cells)


# ---------------------------------------------------------------------------
# Helpers used inside the app
# ---------------------------------------------------------------------------

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

# German labels for the Magic-Find score dimensions (presentation only; the keys
# and their order are owned by magic.MagicScore.dims).
MAGIC_DIM_LABELS = {
    "note": "Tarifnote",
    "leistung_cov": "Leistungs-Breite",
    "module_breadth": "Modul-Breite",
    "coverage_gen": "Deckungs-Großzügigkeit",
    "bewertung": "Kundenbewertung",
}

# -----------------------------------------------------------------------
# Diff helper
# -----------------------------------------------------------------------

def _rows_by_first_key(rows: list[SnapshotRow]) -> dict[str, SnapshotRow]:
    """Map snapshot rows by key, first occurrence wins — snapshot keys legitimately
        repeat (same rationale as _populate_market_table), so join on the first row a
        key names rather than the last duplicate."""
    m: dict[str, SnapshotRow] = {}
    for r in rows:
        m.setdefault(r.key, r)
    return m


def _compute_diff(
    old_snap: Snapshot, new_snap: Snapshot
) -> tuple[list[tuple], list[str], list[str]]:
    """
        Returns (changes, added_keys, removed_keys).
        changes = [(key, old_price, new_price, delta), ...]
        """
    old_map = _rows_by_first_key(old_snap.rows)
    new_map = _rows_by_first_key(new_snap.rows)

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


def _build_verlauf_rows(old_snap: Snapshot, new_snap: Snapshot) -> list[dict]:
    """Build one dict per row for the Verlauf DataTable, joining old and new snapshots by key."""
    old_map = _rows_by_first_key(old_snap.rows)
    new_map = _rows_by_first_key(new_snap.rows)
    rows = []
    for new_row in new_snap.rows:
        old_row = old_map.get(new_row.key)
        old_price = old_row.monatlich_eur if old_row else None
        new_price = new_row.monatlich_eur
        delta_price = (
            (new_price - old_price)
            if (old_price is not None and new_price is not None)
            else None
        )
        delta_pos = (new_row.position - old_row.position) if old_row else None
        rows.append({
            "key": new_row.key,
            "insurer": new_row.insurer,
            "product": new_row.product,
            "sb": new_row.selbstbeteiligung,
            "new_position": new_row.position,
            "old_price": old_price,
            "new_price": new_price,
            "delta_price": delta_price,
            "delta_pos": delta_pos,
            "old_note": old_row.tarifnote if old_row else None,
            "new_note": new_row.tarifnote,
            "is_new": old_row is None,
            "is_removed": False,
        })
    for old_row in old_snap.rows:
        if old_row.key not in new_map:
            rows.append({
                "key": old_row.key,
                "insurer": old_row.insurer,
                "product": old_row.product,
                "sb": old_row.selbstbeteiligung,
                "new_position": None,
                "old_price": old_row.monatlich_eur,
                "new_price": None,
                "delta_price": None,
                "delta_pos": None,
                "old_note": old_row.tarifnote,
                "new_note": None,
                "is_new": False,
                "is_removed": True,
            })
    return rows


_VERLAUF_FILTERS = ["all", "changed", "cheaper", "pricier"]
_VERLAUF_FILTER_LABELS = {
    "all": "Alle",
    "changed": "Geändert",
    "cheaper": "Günstiger",
    "pricier": "Teurer",
}

# ---------------------------------------------------------------------------
# Per-tab widget wiring
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TabSpec:
    """One row of the tab -> widget-id dispatch table. Replaces the several
    copy-pasted per-tab maps (active table, detail band, cursor adoption, focus on
    activation). The table-less panels (Vergleich, Benchmark) carry only a focus_id;
    their table/band/map fields stay None so the lookups skip them cleanly.

    ident_map / rows_map / adopt are CheckApp attribute/method NAMES resolved via
    getattr — the table is module-level, so it can't hold bound instance members."""

    tab_id: str
    focus_id: str                   # widget focused when this tab activates
    table_id: str | None = None     # DataTable id (None for table-less panels)
    band_id: str | None = None      # detail-band container id
    content_id: str | None = None   # detail-band Static id
    ident_map: str | None = None    # attr: ident -> row-key dict
    rows_map: str | None = None      # attr: row-key -> row(/fav/score) dict
    adopt: str | None = None        # method: reconcile active-state from a row-key


TAB_SPECS: dict[str, TabSpec] = {
    "favorites": TabSpec(
        "favorites", "#fav-table", "#fav-table", "#fav-detail", "#fav-detail-content",
        "_fav_ident_to_rk", "_fav_rows", "_adopt_favorites"),
    "market": TabSpec(
        "market", "#market-table", "#market-table", "#detail-panel", "#detail-content",
        "_market_ident_to_rk", "_market_rows", "_adopt_market"),
    "diff": TabSpec("diff", "#diff-panel"),
    "verlauf": TabSpec(
        "verlauf", "#verlauf-table", "#verlauf-table", "#verlauf-detail",
        "#verlauf-detail-content", "_verlauf_ident_to_rk", "_verlauf_rows",
        "_adopt_verlauf"),
    "bench": TabSpec("bench", "#bench-panel"),
    "magic": TabSpec(
        "magic", "#magic-table", "#magic-table", "#magic-detail", "#magic-detail-content",
        "_magic_ident_to_rk", "_magic_rows", "_adopt_magic"),
}

# Tabs that own a selectable row table — the only ones the row-cursor actions
# ([u]/[R]/[a]/[N]/[o]/[D]/[g]/[G]/[H]) and the cross-tab reconcile apply to.
ROW_TABS: tuple[str, ...] = tuple(t for t, s in TAB_SPECS.items() if s.table_id)

# ---------------------------------------------------------------------------
# The App
# ---------------------------------------------------------------------------

class CheckApp(App):
    """check0r3000 — Rechtsschutz-Vergleich TUI."""

    CSS_PATH = Path(__file__).resolve().parent / "tui.tcss"  # resolve: survives symlink launch
    TITLE = "check0r3000 — Rechtsschutz-Vergleich"

    BINDINGS = [
        # Footer (show=True): only the most-used keys; [?] lists everything.
        Binding("y", "switch_tab('favorites')", "Favoriten", show=True),
        Binding("x", "switch_tab('market')", "Markt", show=True),
        Binding("v", "switch_tab('diff')", "Vergleich", show=True),
        Binding("l", "switch_tab('verlauf')", "Verlauf", show=True),
        # Benchmark stays reachable via [B]/tab bar/help — footer space goes to
        # Magic Find, the primary ranking view.
        Binding("B", "switch_tab('bench')", "Benchmark", show=False),
        Binding("M", "switch_tab('magic')", "Magic Find", show=True),
        # Tab cycles tabs forward, Shift+Tab backward. priority=True beats the
        # Screen's default tab->focus_next; action_cycle_tab restores that default
        # when a modal is open or a text field is focused.
        Binding("tab", "cycle_tab(1)", "Nächster Tab", show=False, priority=True),
        Binding("shift+tab", "cycle_tab(-1)", "Voriger Tab", show=False, priority=True),
        Binding("d", "toggle_detail", "Details", show=True),
        Binding("g", "fetch_docs", "Docs+Analyse", show=True),
        Binding("question_mark", "help", "Hilfe", show=True, key_display="?"),
        Binding("q", "quit", "Beenden", show=True),
        # Context / power keys — documented in [?], hidden from the footer.
        Binding("G", "analyze_local", "Nur analysieren", show=False),
        Binding("H", "harvest", "Harvest+Analyse", show=False),
        Binding("P", "toggle_needs", "Bedarf", show=False),
        Binding("W", "edit_needs", "Bedarf-Gewichte", show=False),
        Binding("F", "magic_scan", "Markt-Scan", show=False),
        Binding("U", "update_all", "Update-All", show=False),
        Binding("a", "add_to_compare", "Zum Vergleich", show=False),
        Binding("c", "manage_compare", "Vergleich verwalten", show=False),
        Binding("w", "toggle_compare_wording", "Wortlaut", show=False),
        Binding("t", "compare_fulltext", "Volltext", show=False),
        Binding("o", "open_source", "Quelle öffnen", show=False),
        Binding("O", "open_offer", "Auf CHECK24 öffnen", show=False),
        Binding("f", "focus_filter", "Filter", show=False),
        Binding("escape", "clear_filter", "Zur Tabelle / Filter leeren", show=False),
        Binding("s", "sort_price", "Sortieren: €", show=False),
        Binding("n", "sort_note", "Sortieren: Note", show=False),
        Binding("p", "sort_position", "Sortieren: Position", show=False),
        Binding("j", "sort_changed", "Sortieren: Änderungen", show=False),
        Binding("b", "build_query", "Query-URL", show=False),
        Binding("e", "edit_query", "Suche bearbeiten", show=False),
        Binding("u", "toggle_favorite", "Favorit", show=False),
        Binding("N", "edit_note", "Notiz", show=False),
        Binding("R", "set_reference", "Referenz", show=False),
        Binding("D", "delete_data", "Daten löschen", show=False),
        Binding("r", "refresh_data", "Neu laden", show=True),
        Binding("m", "verlauf_filter", "Verlauf-Filter", show=False),
        Binding("comma", "verlauf_prev_snap", "Älterer Snap", show=False),
        Binding("period", "verlauf_next_snap", "Neuerer Snap", show=False),
        Binding("T", "next_theme", "Theme", show=False),
    ]

    # reactive state
    filter_text: reactive[str] = reactive("", recompose=False)
    sort_col: reactive[str] = reactive("position")
    sort_asc: reactive[bool] = reactive(True)

    def __init__(self, snapshot_path: Path | None) -> None:
        super().__init__()
        self._snapshot_path = snapshot_path
        self._snapshot: Snapshot | None = None
        self._all_snapshots: list[tuple[str, Path]] = []
        self._detail: DetailRecord | None = None
        self._q1 = self._median = self._q3 = 0.0
        self._favorites: dict[str, Any] = {}
        # Per-stem free-text notes live in a gitignored sidecar, NOT in the tracked
        # (PII-free) favorites.json — a typed note is personal, never committed.
        self._favorite_notes: dict[str, str] = {}
        self._doc_index: dict[str, list[dict]] = {}
        self._doc_by_tariff: dict[tuple[str, str], dict] = {}
        self._doc_by_stem: dict[str, dict] = {}
        self._details_by_stem: dict[str, DetailRecord] = {}
        self._filter_timer: Timer | None = None
        self._market_ident_to_rk: dict[str, str] = {}
        self._fav_ident_to_rk: dict[str, str] = {}
        # Cross-tab selection: the tariff ident the user currently has held in
        # the active tab. On a tab switch we re-select it in the newly-active
        # table if present, else leave the cursor as-is. Lazy (switch-time) sync
        # instead of eager real-time sync — you only ever see one tab, so there
        # is no need to move hidden tables, and no echo cascade can form.
        self._held_ident: str | None = None
        self._fav_rows: dict[str, tuple[SnapshotRow, dict]] = {}
        self._market_rows: dict[str, SnapshotRow] = {}
        # Verlauf table row-key -> its row dict, and tariff ident -> row-key, built
        # in _populate_verlauf. Keys are suffixed (#i) so duplicate snapshot keys
        # don't raise DuplicateKey; the ident index drives cross-tab re-selection.
        self._verlauf_rows: dict[str, dict] = {}
        self._verlauf_ident_to_rk: dict[str, str] = {}
        # Magic Find: per-row score (row-key -> MagicScore, insertion order = rank),
        # the ident index for cross-tab re-selection, the representative snapshot row
        # per stem (for the detail band) and a stem -> score lookup for rendering.
        self._magic_rows: dict[str, magic.MagicScore] = {}
        self._magic_ident_to_rk: dict[str, str] = {}
        self._magic_snaprow_by_stem: dict[str, SnapshotRow] = {}
        self._magic_score_by_stem: dict[str, magic.MagicScore] = {}
        # Magic Find Bedarf toggle ([P]): off = objective market quality, on = re-weight
        # module_breadth by the personal needs in config/needs-weights.json.
        self._magic_needs_mode: bool = False
        self._active_row: SnapshotRow | None = None
        self._active_fav: dict | None = None
        # Vergleich tab view state: compact by default (clean ✓/✗/~/— matrix);
        # [w] expands the verbatim per-insurer wording under each category.
        self._compare_verbose: bool = False
        # detail band visibility intent: True = auto-show on navigation,
        # False = user explicitly closed it ([d]); Enter always re-opens.
        self._detail_visible: bool = True
        # Verlauf tab state
        self._verlauf_filter: str = "all"
        self._verlauf_old_idx: int = 0  # index into _all_snapshots for the "from" snapshot
        # Change tracking: loaded once at startup, refreshed on [r]
        self._change_summary: dict[str, ChangeInfo] = {}
        # External test verdicts (Finanztip/F&B/Finanztest) — display-only sidecar
        self._ext_ratings: dict = {}
        # Single-flight guard for the analyze pipeline. @work's exclusive=True only
        # cancels the worker — it cannot abort a blocked subprocess.run, so two
        # pipelines would run ingest/extract concurrently. Refuse a second start.
        self._pipeline_running: bool = False
        # Loader-bar state: the last raw pipeline-status markup (re-rendered by the
        # animation timer with the tui_anim loader bar in front while a pipeline
        # runs) plus the tick counter and an active-flag so the final ✓/✗ line is
        # redrawn once WITHOUT the bar when the run ends.
        self._pipeline_status_markup: str = ""
        self._loader_tick: int = 0
        self._loader_was_active: bool = False

    # --- Lifecycle ---

    def on_mount(self) -> None:
        self._restore_prefs()
        self._load_data()
        self._populate_favorites_table()
        self._populate_market_table()
        self._populate_coverage()
        self._populate_verlauf()
        self._populate_benchmark()
        self._populate_magic()
        self._update_header()
        self._prewarm_analyze_model()
        self._maybe_show_splash()
        self.set_interval(0.09, self._animate_pipeline_status)

    # Personal UI preferences (theme, …) — gitignored sidecar, no PII. Written on
    # every [T] cycle so the chosen theme survives a restart instead of resetting
    # to the default each launch.
    _PREFS_PATH = REPO_ROOT / "config" / "tui-prefs.json"

    def _restore_prefs(self) -> None:
        prefs = load_json_or(self._PREFS_PATH, {})
        theme = prefs.get("theme") if isinstance(prefs, dict) else None
        if isinstance(theme, str) and theme in self._THEMES:
            self.theme = theme

    def _save_prefs(self) -> None:
        try:
            atomic_write_json(self._PREFS_PATH, {"theme": self.theme})
        except OSError as exc:
            self.notify(f"Theme nicht gespeichert: {exc}", severity="warning", timeout=5)

    def _maybe_show_splash(self) -> None:
        """Push the boot animation over the freshly-loaded app. CHECK0R_SPLASH
            picks the variant (1|2|3, default: random per launch) or disables it
            (off); headless runs (tui_test.py, --screenshot) never see it — a
            modal splash would swallow the Pilot's key presses."""
        choice = (os.environ.get("CHECK0R_SPLASH") or "random").strip().lower()
        if self.is_headless or choice in ("off", "0", "no", "none"):
            return
        self.push_screen(SplashScreen(tui_anim.splash_frames(choice)))

    def _load_data(self) -> None:
        """Load snapshot and supplemental data."""
        # Drop the stem-resolution cache first: [H] harvest rewrites the manifest
        # mid-session, and load_snapshot() below resolves every row's stem through
        # it — a stale cache leaves freshly-harvested tariffs unresolved.
        reset_doc_cache()
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
        self._favorite_notes = load_favorite_notes()
        self._migrate_favorite_notes()
        self._doc_index = load_doc_index()
        self._doc_by_tariff = load_doc_by_tariff()
        self._doc_by_stem = {
            t["stem"]: t for t in self._doc_by_tariff.values() if t.get("stem")
        }
        self._details_by_stem = dict(load_all_details())
        self._change_summary = load_change_summary()
        self._ext_ratings = load_external_ratings()
        # invalidate cached offer URL base (profile may have changed)
        if hasattr(self, "_offer_url_cache"):
            del self._offer_url_cache

    def _tariff_key(self, row: "SnapshotRow | None", fav: dict | None = None) -> str | None:
        """Canonical cross-tab matching key for a tariff: row.key when available,
        otherwise built from the fav dict fields (for favorites without a snapshot hit)."""
        if row is not None:
            return row.key
        if fav is not None:
            ins = fav.get("insurer", "")
            prod = fav.get("product", "")
            sb = fav.get("sb", "")
            if ins and prod:
                return f"{ins}|{prod}|{sb}"
        return None

    # --- Layout ---

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(id="tabs", initial="favorites"):
            with TabPane("★ Favoriten [y]", id="favorites"):
                yield Label("", id="fav-knockout")
                with Vertical(id="fav-layout"):
                    yield DataTable(id="fav-table", cursor_type="row", zebra_stripes=True)
                    with ScrollableContainer(id="fav-detail", classes="detail-band"):
                        yield Static(
                            "Favorit wählen — Details, SB-Varianten und Quelldokumente.",
                            id="fav-detail-content",
                        )
            with TabPane("Markt [x]", id="market"):
                yield Input(
                    placeholder="Filter: Versicherer / Produkt…",
                    id="filter-input",
                )
                yield Label(STATUS_LEGEND, id="market-legend")
                with Vertical(id="market-layout"):
                    yield DataTable(id="market-table", cursor_type="row", zebra_stripes=True)
                    with ScrollableContainer(id="detail-panel", classes="detail-band"):
                        yield Static("Zeile wählen für Details.", id="detail-content")
            with TabPane("Vergleich [v]", id="diff"):
                with ScrollableContainer(id="diff-panel"):
                    yield Static("Vergleich wird geladen…", id="diff-content")
            with TabPane("Verlauf [l]", id="verlauf"):
                yield Label("", id="verlauf-header")
                with Vertical(id="verlauf-layout"):
                    yield DataTable(id="verlauf-table", cursor_type="row", zebra_stripes=True)
                    with ScrollableContainer(id="verlauf-detail", classes="detail-band"):
                        yield Static(
                            "Zeile wählen für Details.",
                            id="verlauf-detail-content",
                        )
            with TabPane("Benchmark [B]", id="bench"):
                with ScrollableContainer(id="bench-panel"):
                    yield Static("Benchmark wird geladen…", id="bench-content")
            with TabPane("✨ Magic Find [M]", id="magic"):
                yield Label("", id="magic-header")
                with Vertical(id="magic-layout"):
                    yield DataTable(id="magic-table", cursor_type="row", zebra_stripes=True)
                    with ScrollableContainer(id="magic-detail", classes="detail-band"):
                        yield Static(
                            "Magic-Find-Zeile wählen für den Score-Beitrag je Dimension.",
                            id="magic-detail-content",
                        )
        yield Label("", id="status-bar")
        yield Footer()
        # Fat centered loader overlay, shown while a pipeline runs. Toast pattern:
        # the full-screen layer container is visibility:hidden (clicks fall through
        # to the widgets beneath), only the box inside is visible.
        with Container(id="loader-layer"):
            yield Static("", id="loader-overlay")

    # --- Header update ---

    def _update_header(self) -> None:
        if self._snapshot:
            self.sub_title = (
                f"{self._snapshot.date}  |  {self._snapshot.profile}"
                f"  |  {len(self._snapshot.rows)} Tarife"
            )
        else:
            self.sub_title = "Kein Snapshot geladen — [U] Update-All erstellt einen"

    def _update_status_bar(self) -> None:
        t = datetime.datetime.now().strftime("%H:%M:%S")
        n = len(self._snapshot.rows) if self._snapshot else 0
        try:
            self.query_one("#status-bar", Label).update(
                f"[dim]Neu geladen: {t}  ·  {n} Tarife  ·  [bold]r[/bold] Reload[/dim]"
            )
        except NoMatches:
            pass

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
        delta = price_delta(price, ref_price)
        if delta is None:
            return "[dim]—[/dim]"
        d, pct, color, sign = delta
        if abs(d) < 0.005:
            return "[dim]±0[/dim]"
        approx = "≈" if (sb or "") != (ref_sb or "") else ""
        return f"[{color}]{approx}{sign}{d:.2f} ({sign}{pct:.0f}%)[/{color}]"

    def _docs_label(self, stem: str) -> str:
        seen: list[str] = []
        for dd in self._doc_index.get(stem, []):
            # doctype fallback = raw manifest data (harvest passes unknown
            # CHECK24 kinds through verbatim) — escape before cell markup
            lbl = _esc(_DOCTYPE_SHORT.get(dd.get("doctype", ""),
                                          dd.get("doctype", "")))
            if lbl and lbl not in seen:
                seen.append(lbl)
        return "·".join(seen) if seen else "[dim]—[/dim]"

    def _update_fav_banner(self, ref_price: float | None, ref_sb: str | None) -> None:
        try:
            ko = self.query_one("#fav-knockout", Label)
        except NoMatches:
            return
        parts: list[str] = []
        ko_text = self._favorites.get("knockout", "")
        if ko_text:
            parts.append(f"⊘ {ko_text}")
        ref_row = self._reference_row()
        if ref_row is not None and ref_price is not None:
            parts.append(
                f"◆ Referenz: {_esc(ref_row.insurer)} {_esc(ref_row.product)} "
                f"(SB {_esc(ref_sb)}, €{ref_price:.2f}/mo) — \\[R] setzt eine andere; "
                f"Δ vergleicht dagegen, ≈ markiert eine abweichende SB-Stufe (nicht 1:1)."
            )
        parts.append(
            "↵ Detail · \\[O] auf CHECK24 öffnen · \\[R] Referenz · \\[u] Favorit"
        )
        parts.append(STATUS_LEGEND)
        ko.update(guard_content("\n".join(parts)))

    def _fav_row_cells(
        self,
        fav: dict,
        row: "SnapshotRow | None",
        variants: "list[SnapshotRow]",
        ref_price: float | None,
        ref_sb: str | None,
    ) -> tuple[str, ...]:
        if row is None:
            return (
                "[dim]?[/dim]",
                _esc(fav.get("insurer") or ""),
                _esc(fav.get("product") or ""),
                "—", "—", "—", "—", "—",
                self._docs_label(fav.get("stem", "")),
            )
        if self._is_reference(fav):
            star = "[bright_yellow]◆[/bright_yellow]"
        elif fav.get("recommended"):
            star = "[bright_green]▶[/bright_green]"
        else:
            star = "[yellow]★[/yellow]"
        nc = _tarifnote_color(row.tarifnote)
        note_col = f"[{nc}]{_esc(row.tarifnote)}[/{nc}]" if row.tarifnote else "—"
        price_str = f"{row.monatlich_eur:.2f}" if row.monatlich_eur is not None else "—"
        pc = _price_color(row.monatlich_eur, self._q1, self._q3)
        price_col = f"[{pc}]{price_str}[/{pc}]"
        delta_col = (
            "[dim]— (Referenz)[/dim]"
            if self._is_reference(fav)
            else self._delta_cell(row.monatlich_eur, row.selbstbeteiligung, ref_price, ref_sb)
        )
        sb_cell = _esc(row.selbstbeteiligung or "—")
        if len(variants) > 1:
            sb_cell = f"{sb_cell} [dim]·{len(variants)}▾[/dim]"
        docs_cell = f"{_status_glyph(row)} {self._docs_label(fav.get('stem', ''))}"
        return (star, _esc(row.insurer), _esc(row.product), note_col,
                _bewertung_cell(row, *self._bew_lohi()),
                price_col, sb_cell, delta_col, docs_cell)

    def _bew_lohi(self) -> tuple[float | None, float | None]:
        """Snapshot-wide customer-rating min/max for the data-relative bewertung colour."""
        snap = self._snapshot
        if snap is not None:
            return snap.bewertung_lo, snap.bewertung_hi
        return None, None

    def _populate_favorites_table(self) -> None:
        try:
            table: DataTable = self.query_one("#fav-table", DataTable)
        except NoMatches:
            return

        table.clear(columns=True)
        table.add_columns("★", "Versicherer", "Produkt", "Note", "Bew.", "€/mo", "SB", "Δ Ref", "Status")
        self._fav_rows = {}

        ref_price, ref_sb = self._reference_info()
        self._update_fav_banner(ref_price, ref_sb)

        if not self._snapshot:
            return

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
            key = f"fav-{idx}"
            self._fav_rows[key] = (row, fav)
            table.add_row(*guard_cells(
                self._fav_row_cells(fav, row, variants, ref_price, ref_sb)), key=key)

        self._fav_ident_to_rk = {}
        for rk, (row, fav) in self._fav_rows.items():
            ident = self._tariff_key(row, fav)
            if ident:
                self._fav_ident_to_rk.setdefault(ident, rk)

    def _fav_detail_header(self, row: SnapshotRow, fav: dict) -> list[str]:
        lines: list[str] = []
        lines.append(f"[bold]{_esc(row.insurer)}[/bold] — [italic]{_esc(row.product)}[/italic]")
        url = self._build_offer_url(row.position) if row.position else None
        if url:
            lines.append(
                f'[link="{link_url(url)}"][cyan]↗ auf CHECK24 ansehen[/cyan][/link]'
                f"   [dim](Position {row.position})[/dim]"
            )
        tag = fav.get("tag", "")
        if tag or self._is_reference(fav):
            if self._is_reference(fav):
                marker, mcolor = "◆", "bright_yellow"
            elif fav.get("recommended"):
                marker, mcolor = "▶", "bright_green"
            else:
                marker, mcolor = "★", "yellow"
            lines.append(f"[{mcolor}]{marker} {_esc(tag) or 'Referenz'}[/{mcolor}]")
        lines.append("")
        return lines

    def _fav_detail_note(self, fav: dict) -> list[str]:
        note = self._favorite_notes.get(fav.get("stem", ""), "")
        if note:
            lines: list[str] = [
                "[bold underline]Notiz[/bold underline]   [dim](\\[N] bearbeiten)[/dim]"
            ]
            for nl in note.splitlines() or [note]:
                lines.append(f"  [italic]{_esc(nl)}[/italic]")
        else:
            lines = ["[dim]\\[N] Notiz hinzufügen[/dim]"]
        lines.append("")
        return lines

    def _fav_detail_pricing(self, row: SnapshotRow, fav: dict) -> list[str]:
        lines: list[str] = []
        nc = _tarifnote_color(row.tarifnote)
        lines.append(f"Tarifnote : [{nc}]{_esc(row.tarifnote or '—')}[/{nc}]   "
                     "[dim](Experten-Note)[/dim]")
        if row.bewertung is not None:
            lines.append(f"Bewertung : {_bewertung_cell(row, *self._bew_lohi())}   "
                         "[dim](Kundenbewertung /5)[/dim]")
        price = f"{row.monatlich_eur:.2f}" if row.monatlich_eur is not None else "—"
        lines.append(
            f"€/Monat   : [bright_green]{price}[/bright_green]   "
            f"(SB {_esc(row.selbstbeteiligung or '—')})"
        )
        ref_price, ref_sb = self._reference_info()
        delta = price_delta(row.monatlich_eur, ref_price)
        if delta is not None and not self._is_reference(fav):
            d, pct, color, sign = delta
            if abs(d) < 0.005:
                lines.append("vs. Referenz: [dim]±0 €/mo[/dim]")
            else:
                lines.append(
                    f"vs. Referenz: [{color}]{sign}{d:.2f} €/mo ({sign}{pct:.0f}%)[/{color}]"
                )
                if (row.selbstbeteiligung or "") != (ref_sb or ""):
                    lines.append(
                        f"  [yellow]≈ andere SB-Stufe[/yellow] [dim]({_esc(row.selbstbeteiligung)} "
                        f"vs. Referenz {_esc(ref_sb)}) — nicht 1:1[/dim]"
                    )
        lines.append("")
        return lines

    def _fav_detail_variants(self, row: SnapshotRow, fav: dict) -> list[str]:
        # _snapshot can be None if the snapshot file vanished while a fav row was
        # still the active detail target (DataTable.clear() fires no RowHighlighted).
        # match_favorite() dereferences snapshot.rows, so guard before calling.
        variants: list[SnapshotRow] = []
        if self._snapshot is not None:
            _, variants = match_favorite(self._snapshot, fav)
        if len(variants) <= 1:
            return []
        lines = ["[underline]SB-Varianten[/underline]"]
        for v in sorted(
            variants, key=lambda r: r.monatlich_eur if r.monatlich_eur is not None else 9999.0
        ):
            p = f"{v.monatlich_eur:.2f}" if v.monatlich_eur is not None else "—"
            mark = " [bright_yellow]◀ angezeigt[/bright_yellow]" if v.key == row.key else ""
            lines.append(f"  {_esc(v.selbstbeteiligung):<18} €{p}{mark}")
        lines.append("")
        return lines

    def _fav_detail_docs(self, fav: dict, detail_rec: "DetailRecord | None") -> list[str]:
        docs = self._doc_index.get(fav.get("stem", ""), [])
        if not docs:
            return []
        lines = ["[underline]Quelldokumente (URLs gesichert)[/underline]"]
        for dd in docs:
            lbl = _esc("{:<6}".format(
                _DOCTYPE_SHORT.get(dd.get("doctype", ""), dd.get("doctype", ""))))
            fname = _esc((dd.get("file") or "")[:54])
            doc_url = dd.get("url") or ""
            if doc_url:
                lines.append(f'  [cyan]{lbl}[/cyan] [link="{link_url(doc_url)}"]{fname}[/link]')
            else:
                lines.append(f"  [cyan]{lbl}[/cyan] {fname}")
        if detail_rec is None:
            lines.append(
                "[bright_yellow]  \\[g] herunterladen + analysieren"
                "   ·   \\[G] nur analysieren (PDFs lokal)[/bright_yellow]"
            )
            lines.append(
                f"  [dim]→ fetch_docs.py {fav.get('stem')} --into-raw"
                " → ingest → extract[/dim]"
            )
        lines.append("")
        return lines

    def _render_favorite_detail(self, row: SnapshotRow, fav: dict) -> str:
        detail_rec = self._detail_for_row(row)
        lines: list[str] = [
            *self._fav_detail_header(row, fav),
            *self._fav_detail_note(fav),
            *self._fav_detail_pricing(row, fav),
            *self._fav_detail_variants(row, fav),
            *self._fav_detail_docs(fav, detail_rec),
        ]
        if detail_rec is not None:
            lines.append(
                "[bold underline]Tarifdetails[/bold underline]   "
                "[dim](\\[o] Quelle öffnen · \\[v] Vergleich)[/dim]"
            )
            lines += record_body_lines(detail_rec)
        else:
            lines.append(
                "[dim italic]Noch keine AVB/PIB eingelesen — \\[g] lädt + analysiert "
                "sie für den Modul-Vergleich.[/dim italic]"
            )
        return "\n".join(lines)

    # --- Market table ---

    def _change_cell(self, stem: str | None) -> str:
        """Rich markup cell for the Δ (change count) column in the market table."""
        if not stem:
            return "[dim]—[/dim]"
        ci = self._change_summary.get(stem)
        if ci is None:
            return "[dim]—[/dim]"
        total = ci.feature_changes + ci.price_changes
        if total == 0:
            return "[dim]·[/dim]"
        lcd = ci.last_change_date
        recent = False
        if lcd:
            try:
                recent = (datetime.date.today() - datetime.date.fromisoformat(lcd)).days <= 30
            except (ValueError, TypeError):
                pass
        color = "bright_yellow" if recent else "cyan"
        return f"[{color}]Δ{total}[/{color}]"

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
        elif key == "changed":
            def changed_key(r: SnapshotRow) -> tuple:
                ci = self._change_summary.get(r.stem or "")
                if ci is None:
                    return ("", 0)
                return (ci.last_change_date or "", ci.feature_changes + ci.price_changes)
            rows.sort(key=changed_key, reverse=not self.sort_asc)
        return rows

    def _populate_market_table(self) -> None:
        try:
            table: DataTable = self.query_one("#market-table", DataTable)
        except NoMatches:
            return

        table.clear(columns=True)
        # Sort feedback: the active sort column carries a direction arrow, so a
        # [s]/[n]/[p]/[j] press (or header click) is visible instead of a silent
        # re-order. Index map matches on_header_selected's col_map.
        labels = ["#", "St", "Versicherer", "Produkt", "Note", "Bew.", "€/mo", "SB", "Δ"]
        sort_idx = {"position": 0, "insurer": 2, "note": 4, "price": 6, "changed": 8}.get(
            self.sort_col
        )
        if sort_idx is not None:
            labels[sort_idx] += " ▲" if self.sort_asc else " ▼"
        table.add_columns(*labels)

        rows = self._visible_rows()
        row_count_label = self.query_one("#filter-input", Input)
        # update placeholder with count
        row_count_label.placeholder = (
            f"Filter: Versicherer / Produkt… ({len(rows)} angezeigt)"
        )
        try:
            date = self._snapshot.date if self._snapshot else "?"
            self.query_one("#market-legend", Label).update(
                f"{STATUS_LEGEND}   [dim]· Δ Änderungen (\\[j]) · gelesen am {date}[/dim]"
            )
        except NoMatches:
            pass

        # Build a unique DataTable key per row and a key->row map for the
        # highlight handler. r.key (insurer|product|SB) can legitimately repeat —
        # snapshot.py itself counts same-key rows — so an enumerate suffix keeps
        # add_row from raising DuplicateKey on mount/filter/sort.
        self._market_rows = {}
        for i, r in enumerate(rows):
            star = _status_glyph(r)

            note_col = (
                f"[{_tarifnote_color(r.tarifnote)}]{_esc(r.tarifnote)}"
                f"[/{_tarifnote_color(r.tarifnote)}]"
                if r.tarifnote
                else "—"
            )

            price_str = f"{r.monatlich_eur:.2f}" if r.monatlich_eur is not None else "—"
            price_col = (
                f"[{_price_color(r.monatlich_eur, self._q1, self._q3)}]{price_str}[/{_price_color(r.monatlich_eur, self._q1, self._q3)}]"
            )

            row_key = f"{r.key or r.position}#{i}"
            self._market_rows[row_key] = r
            table.add_row(
                *guard_cells((
                    str(r.position),
                    star,
                    _esc(r.insurer),
                    _esc(r.product),
                    note_col,
                    _bewertung_cell(r, *self._bew_lohi()),
                    price_col,
                    _esc(r.selbstbeteiligung or "—"),
                    self._change_cell(r.stem),
                )),
                key=row_key,
            )

        # First occurrence wins, so a held ident re-selects the same row the user
        # saw rather than the last duplicate (snapshot keys can repeat).
        self._market_ident_to_rk = {}
        for rk, r in self._market_rows.items():
            if r.key:
                self._market_ident_to_rk.setdefault(r.key, rk)

    # --- Detail panel (sidebar) ---

    def _doc_entry(self, row: SnapshotRow) -> dict | None:
        """Resolve a snapshot row to its harvested manifest entry (stem + docs)
            via the row's canonical stem (computed once at load via the same manifest
            join). None when the tariff has no harvested source URLs."""
        if row.stem:
            return self._doc_by_stem.get(row.stem)
        return None

    def _equivalent_analyzed(
        self, row: SnapshotRow
    ) -> tuple[dict, DetailRecord] | None:
        """A DIFFERENT-insurer manifest tariff with the SAME product name that is
            already analyzed — e.g. S-Direkt and BavariaDirekt both resell ÖRAG's
            'Rundum-Schutz (ÖRAG Rechtsschutz)'. CHECK24 product names carry the
            risk-carrier, so an exact (case-insensitive) product match across a
            different insurer is the same underlying terms. Distinct names like
            '… mit Beitragsgarantie' or '… - PLUS' are real variants and do NOT
            match. A hint only (loads nothing), returned solely when exactly one such
            analyzed twin exists — so a stray generic-name collision can't mislead."""
        prod = (row.product or "").strip().casefold()
        own = (row.insurer or "").strip().casefold()
        if not prod:
            return None
        by_stem = self._details_by_stem
        hits: list[tuple[dict, DetailRecord]] = []
        for (ins_cf, prod_cf), entry in self._doc_by_tariff.items():
            if prod_cf != prod or ins_cf == own:
                continue
            det = by_stem.get(entry.get("stem"))
            if det is not None:
                hits.append((entry, det))
        return hits[0] if len(hits) == 1 else None

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
                # The doctype fallback is raw manifest data — harvest_docs passes
                # unknown CHECK24 `kind`s through verbatim, so escape it (pad
                # first: the escape backslash renders zero-width).
                raw_lbl = _DOCTYPE_SHORT.get(dd.get("doctype", ""),
                                             dd.get("doctype", ""))
                lbl = _esc(f"{raw_lbl:<6}")
                fname = _esc((dd.get("file") or "")[:60])
                doc_url = dd.get("url") or ""
                if doc_url:
                    lines.append(
                        f'  [cyan]{lbl}[/cyan] [link="{link_url(doc_url)}"]{fname}[/link]'
                    )
                else:
                    lines.append(f"  [cyan]{lbl}[/cyan] {fname}")
            if detail:
                lines.append("[bright_green]  ✓ analysiert[/bright_green]")
            else:
                lines.append(
                    "[bright_yellow]  \\[g] herunterladen + analysieren"
                    "   ·   \\[G] nur analysieren (PDFs lokal)[/bright_yellow]"
                )
                lines.append(
                    f"  [dim]→ fetch_docs.py {_esc(str(entry.get('stem') or ''))}"
                    " --into-raw → ingest → extract[/dim]"
                )
        elif not detail:
            twin = self._equivalent_analyzed(row)
            if twin is not None:
                entry, _det = twin
                lines.append(
                    "[bright_cyan]→ Gleiches Produkt schon analysiert:[/bright_cyan] "
                    f"[bold]{_esc(entry.get('insurer', ''))}[/bold]"
                    f" — {_esc(entry.get('tariff', ''))}"
                )
                lines.append(
                    "[dim]  Gleicher Produktname bei anderem Vertrieb = identischer"
                    " Tarif/Wortlaut; meist unterscheidet sich nur der Preis. Dort im"
                    " Markt die Details ansehen (oder \\[a] für den Vergleich).[/dim]"
                )
            else:
                lines.append(
                    "[dim]Quell-PDFs noch nicht geharvestet —[/dim] "
                    "[bright_yellow]\\[H] live harvesten + analysieren[/bright_yellow]"
                    "[dim] (lädt die CHECK24-Seite headless).[/dim]"
                )
        return "\n".join(lines)

    def _detail_for_row(self, row: SnapshotRow) -> "DetailRecord | None":
        """Resolve a SnapshotRow to its DetailRecord: cache-hit when the row has a
        stem, disk-fallback (_load_detail) for legacy records without one."""
        if row.stem and row.stem in self._details_by_stem:
            return self._details_by_stem[row.stem]
        return _load_detail(row.insurer, row.product)

    def _render_change_history_block(self, row: SnapshotRow) -> str:
        """Compact Änderungshistorie section for the market detail band."""
        stem = row.stem
        if not stem:
            return ""
        ci = self._change_summary.get(stem)
        if ci is None:
            return ""
        today = datetime.date.today()
        total = ci.feature_changes + ci.price_changes
        lad = ci.last_analysis_date

        stale_warn = ""
        if lad:
            try:
                days = (today - datetime.date.fromisoformat(lad)).days
                if days > 60:
                    stale_warn = f"[yellow]⚠ Letzte Analyse: {lad} ({days} Tage)[/yellow]"
            except (ValueError, TypeError):
                pass

        lines: list[str] = []

        if total == 0:
            if lad:
                fsd = ci.first_seen_date or lad
                lines.append(
                    f"[bold underline]Änderungen[/bold underline]   "
                    f"[dim]analysiert seit {fsd}, keine Änderungen erkannt[/dim]"
                )
            if stale_warn:
                lines.append(f"  {stale_warn}")
            return "\n".join(lines) if lines else ""

        lines.append("[bold underline]Änderungshistorie[/bold underline]")
        if stale_warn:
            lines.append(f"  {stale_warn}")

        if ci.feature_changes:
            pl = "en" if ci.feature_changes != 1 else ""
            lines.append(
                f"  Leistungen: [cyan]{ci.feature_changes} Änderung{pl}[/cyan]"
            )
            for old_d, new_d, diff in ci.feature_changelog:
                parts: list[str] = []
                if diff.get("leistungen"):
                    for a in diff["leistungen"].get("added", [])[:3]:
                        parts.append(f"[bright_green]+{_esc(a[:38])}[/bright_green]")
                    for r in diff["leistungen"].get("removed", [])[:2]:
                        parts.append(f"[bright_red]−{_esc(r[:38])}[/bright_red]")
                if diff.get("modules"):
                    for ch in diff["modules"][:2]:
                        lbl = MODULE_LABELS.get(ch["key"], ch["key"])
                        if ch["old_included"] != ch["new_included"]:
                            col = "bright_green" if ch["new_included"] else "bright_red"
                            gl = "+" if ch["new_included"] else "−"
                            parts.append(f"[{col}]{gl}Modul {lbl}[/{col}]")
                        elif ch["old_level"] != ch["new_level"]:
                            parts.append(
                                f"[cyan]{lbl}: {ch['old_level'] or '—'}→{ch['new_level'] or '—'}[/cyan]"
                            )
                summary = "  ".join(parts) if parts else "Änderung erkannt"
                lines.append(f"    {new_d}: {summary}")

        if ci.price_changes:
            pl = "en" if ci.price_changes != 1 else ""
            lines.append(
                f"  Preis: [cyan]{ci.price_changes} Änderung{pl}[/cyan]"
            )
            for ch in ci.price_changelog[-3:]:
                sign = "+" if ch["delta"] > 0 else ""
                col = "bright_red" if ch["delta"] > 0 else "bright_green"
                lines.append(
                    f"    {ch['date']}: "
                    f"{ch['old_price']:.2f} → {ch['new_price']:.2f}  "
                    f"[{col}]{sign}{ch['delta']:.2f}[/{col}]"
                )

        return "\n".join(lines) if len(lines) > 1 else ""

    def _render_market_detail(self, row: SnapshotRow) -> str:
        """Full tariff detail (modules, coverage, premium, benefits, exclusions)
            plus the source-document / [g] block, for the inline Market band."""
        detail = self._detail_for_row(row)
        parts = [self._render_detail_full(row, detail)]
        ext = external_ratings_for(row.stem, row.insurer, self._ext_ratings)
        if ext:
            parts.append("\n".join(external_rating_lines(ext)))
        docs = self._render_docs_block(row, detail)
        if docs:
            parts.append(docs)
        change_hist = self._render_change_history_block(row)
        if change_hist:
            parts.append(change_hist)
        result = "\n\n".join(parts)
        url = self._build_offer_url(row.position) if row.position else None
        if url:
            link_line = (
                f'[link="{link_url(url)}"][cyan]↗ auf CHECK24 ansehen[/cyan][/link]'
                f"   [dim](Position {row.position})[/dim]"
            )
            result = link_line + "\n\n" + result
        return result

    # --- Full detail tab ---

    def _render_detail_full(self, row: SnapshotRow, detail: "DetailRecord | None" = None) -> str:
        if detail is None:
            detail = _load_detail(row.insurer, row.product)
        if not detail:
            head = f"[bold]{_esc(row.insurer)}[/bold] — {_esc(row.product)}\n\n"
            if self._equivalent_analyzed(row) is not None:
                # The same product is analyzed under another distributor; the docs
                # block below names it, so don't tell the user to run ingest here.
                return (
                    head + "[dim italic]Kein eigener Datensatz — gleiches Produkt"
                    " ist unter einem anderen Vertrieb analysiert (siehe unten)."
                    "[/dim italic]"
                )
            # Only point at [G] when the PDFs are actually on disk; otherwise it's
            # a dead end (nothing to extract). The docs block below states the real
            # next step: [g] download, or the browser harvest when there are no
            # source URLs at all (e.g. KS/Auxilia — never harvested).
            ingest_hint = (
                "\n[dim]PDFs liegen lokal — \\[G] analysiert sie (ingest → extract)."
                "[/dim]" if row.has_pdf else ""
            )
            return (
                head + "[dim italic]Noch nicht analysiert.[/dim italic]"
                + ingest_hint
            )

        lines: list[str] = []
        badge = (
            "[bright_green]angereicherter Record (mit Beitrag/Stufe)[/bright_green]"
            if detail.is_enriched
            else "[cyan]Basis-Record[/cyan]"
        )
        lines.append(f"[bold underline]{_esc(detail.insurer)}[/bold underline]  — {badge}")
        lines.append(f"[bold]{_esc(detail.tariff)}[/bold]")
        if detail.stand:
            lines.append(f"Stand: {_esc(detail.stand)}")
        lines.append("")
        lines += record_body_lines(detail)
        return "\n".join(lines)

    # --- Vergleich tab (cross-tariff coverage comparison) ---

    def _compare_stems(self) -> list[str]:
        """The tariffs explicitly chosen for the Vergleich — an include-set in
            config/favorites.json, curated separately from the favorites star
            (Market [a] adds/removes, [c] bulk-manages). If the key is absent (e.g. a
            fresh favorites.json) it seeds from the favorite stems, so the comparison
            starts as the favorites and is curated from there.
            Read-only: the seed is persisted only when [a]/[c] next write it."""
        stems = self._favorites.get("compare_stems")
        if stems is None:
            stems = [f["stem"] for f in self._favorites.get("favorites", [])
                     if f.get("stem")]
        return list(stems)

    def _set_compare_stems(self, stems: list[str]) -> None:
        """Persist the curated compare-set (the include-set the Vergleich renders)."""
        self._favorites["compare_stems"] = stems
        self._save_favorites()

    def _coverage_columns(self) -> list[tuple[str, DetailRecord]]:
        """Analyzed records for the curated compare-set as (stem, record),
            reference_stem first then by stem — so the current contract ([R]) is
            always the leftmost baseline column. Compare-set members that aren't
            analyzed yet are skipped here (counted as pending by _populate_coverage)."""
        included = self._compare_stems()
        by_stem = self._details_by_stem
        ref = self._favorites.get("reference_stem")
        cols = [(s, by_stem[s]) for s in included if s in by_stem]
        cols.sort(key=lambda sr: (sr[0] != ref, sr[0]))
        return cols

    def _is_ref_col(self, stem: str) -> bool:
        return stem == self._favorites.get("reference_stem")

    def _col_header(self, cols: list[tuple[str, DetailRecord]], col_w: int,
                    title: str) -> str:
        head = _pad_cell(title, VERGLEICH_LABEL_W)  # same truncation rule as rows
        for stem, _ in cols:
            lbl = _col_label(stem) + (" (Ref)" if self._is_ref_col(stem) else "")
            head += _pad_cell(lbl, col_w)
        return f"[bold underline]{head}[/bold underline]"

    def _populate_benchmark(self) -> None:
        """Render the durable benchmark digest (benchmarks/results.json) as a ranked,
        colour-coded scorecard in the Benchmark tab. Scoring shares the same formula
        as the eval CLI via scorecard.scored_by_tariff (the module-coverage denominator
        is batch-relative, so points can differ when the row sets differ). Absent or
        malformed digest -> a message instead of crashing the mount."""
        try:
            widget: Static = self.query_one("#bench-content", Static)
        except NoMatches:
            return
        try:
            results = scorecard.load_results()
            groups = list(scorecard.scored_by_tariff(results.get("rows", [])))
            widget.update(guard_content(benchmark_markup(results, groups)))
        except Exception as exc:  # a hand-edited / old-schema digest must not kill mount
            widget.update(
                f"[bright_red]benchmarks/results.json ist unlesbar:[/bright_red] "
                f"{_esc(exc)}\n[dim]Erwartet das Schema aus scorecard.py / eval.py "
                "--save-summary.[/dim]"
            )

    def _populate_coverage(self) -> None:
        try:
            widget: Static = self.query_one("#diff-content", Static)
        except NoMatches:
            return

        cols = self._coverage_columns()
        included = self._compare_stems()
        analyzed = set(self._details_by_stem)
        pending = [s for s in included if s not in analyzed]
        if not cols:
            if pending:
                names = ", ".join(_col_label(s) for s in pending)
                widget.update(guard_content(
                    "[dim italic]Im Vergleich vorgemerkt, aber noch nicht "
                    f"analysiert: {_esc(names)}.\n"
                    "Markiere den Tarif im Markt und drücke \\[g] (Download + Analyse) "
                    "— dann erscheint seine Spalte hier.[/dim italic]"
                ))
            else:
                widget.update(
                    "[dim italic]Der Vergleich ist leer.\n"
                    "Markiere im Markt einen Tarif und drücke \\[a] — er wird dem "
                    "Vergleich hinzugefügt und bei Bedarf automatisch analysiert. "
                    "\\[c] verwaltet die Auswahl.[/dim italic]"
                )
            return

        # Cap the columns to what fits the panel at the minimum column width: more
        # tariffs than fit would push every matrix row past the terminal edge and
        # wrap, re-breaking the alignment this view exists to provide. The reference
        # column is leftmost (sorted first), so it always survives the cap; the rest
        # can be chosen via [c]. avail falls back to 130 before the first layout pass.
        width = self.size.width or 130
        avail = max(60, width - 6)
        max_cols = max(1, (avail - VERGLEICH_LABEL_W) // 13)
        shown = cols[:max_cols]
        overflow = len(cols) - len(shown)
        col_w = _vergleich_col_w(len(shown), avail)

        # Snapshot rows keyed by stem for the MARKTDATEN section.
        snap_by_stem: dict[str, SnapshotRow] = {}
        if self._snapshot:
            for r in self._snapshot.rows:
                if r.stem:
                    snap_by_stem[r.stem] = r

        mode = "Wortlaut an \\[w]" if self._compare_verbose else "kompakt · \\[w] Wortlaut"
        pending_hint = (
            f" · [yellow]{len(pending)} ausstehend (\\[g] analysieren)[/yellow]"
            if pending else ""
        )
        overflow_hint = (
            f" · [yellow]+{overflow} passen nicht — \\[c] verwalten / Terminal "
            f"breiter[/yellow]" if overflow else ""
        )
        parts = [
            "[bold]Tarif-Vergleich[/bold]   "
            f"[dim]{len(shown)}/{len(cols)} Tarife · Referenz \\[R] links · {mode} · "
            f"\\[a] Tarif zufügen · \\[t] Volltext · \\[c] verwalten · \\[o] Quelle öffnen"
            f"{pending_hint}{overflow_hint}[/dim]",
            self._render_market_matrix(shown, col_w, snap_by_stem),
            self._render_module_matrix(shown, col_w),
            self._render_coverage_matrix(shown, col_w),
            self._render_category_matrix("leistung", shown, col_w),
            self._render_category_matrix("ausschluss", shown, col_w),
            "[dim]Legende: [green]✓[/green] enthalten · [red]✗[/red] ausgeschlossen · "
            "[yellow]~[/yellow] teilweise (nur/eingeschr./außer/begrenzt) · "
            "[dim]—[/dim] nicht genannt[/dim]",
        ]
        tail = self._render_snapshot_pricediff()
        if tail:
            parts.append(tail)
        widget.update(guard_content("\n\n".join(parts)))

    def _render_market_matrix(self, cols, col_w, snap_by_stem: dict) -> str:
        """Snapshot-sourced market data for the compared tariffs: price, expert
        grade, customer rating, and per-Baustein wait times. Placed first in the
        Vergleich so the fast-decision numbers are immediately visible without
        scrolling past the AVB-analysis sections."""
        lines = [self._col_header(cols, col_w, "MARKTDATEN")]

        # Per-column CHECK24 links — built as markup (not through _pad_cell, which
        # would escape the [link=…] tags). Padding is manual: link markup is
        # invisible-width, only the display text counts.
        link_row = _pad_label("↗ CHECK24")
        has_any_link = False
        for stem, _ in cols:
            srow = snap_by_stem.get(stem)
            url = self._build_offer_url(srow.position) if (srow and srow.position) else None
            if url:
                display = "↗ Link"
                pad = " " * max(0, col_w - len(display))
                link_row += f'[link="{link_url(url)}"][cyan]{display}[/cyan][/link]{pad}'
                has_any_link = True
            else:
                link_row += _pad_cell("—", col_w, "dim")
        if has_any_link:
            lines.append(link_row)

        row = _pad_label("€/Monat")
        for stem, _ in cols:
            srow = snap_by_stem.get(stem)
            if srow and srow.monatlich_eur is not None:
                pc = _price_color(srow.monatlich_eur, self._q1, self._q3)
                row += _pad_cell(f"{srow.monatlich_eur:.2f}", col_w, pc)
            else:
                row += _pad_cell("—", col_w, "dim")
        lines.append(row)

        row = _pad_label("Tarifnote")
        for stem, _ in cols:
            srow = snap_by_stem.get(stem)
            if srow and srow.tarifnote:
                row += _pad_cell(srow.tarifnote, col_w, _tarifnote_color(srow.tarifnote))
            else:
                row += _pad_cell("—", col_w, "dim")
        lines.append(row)

        row = _pad_label("Kundenbewertung ★")
        for stem, _ in cols:
            srow = snap_by_stem.get(stem)
            if srow and srow.bewertung is not None:
                v = srow.bewertung
                cnt = f" ({srow.bewertung_anzahl})" if srow.bewertung_anzahl else ""
                color = _bewertung_color(v, *self._bew_lohi())
                row += _pad_cell(f"{v:.1f}★{cnt}", col_w, color)
            else:
                row += _pad_cell("—", col_w, "dim")
        lines.append(row)

        # Collect all Baustein names present across compared tariffs, in appearance order.
        module_keys: list[str] = []
        for stem, _ in cols:
            srow = snap_by_stem.get(stem)
            if srow and srow.wartezeit_per_modul:
                for k in srow.wartezeit_per_modul:
                    if k not in module_keys:
                        module_keys.append(k)
        for modul in module_keys:
            row = _pad_label(f"Wartezeit {modul}")
            for stem, _ in cols:
                srow = snap_by_stem.get(stem)
                wz = (srow.wartezeit_per_modul or {}).get(modul) if srow else None
                if wz:
                    try:
                        monate = int(wz.split()[0])
                        color: str | None = (
                            "bright_green" if monate == 0
                            else "yellow" if monate <= 3
                            else "dim"
                        )
                    except (ValueError, IndexError):
                        color = None
                    row += _pad_cell(wz, col_w, color)
                else:
                    row += _pad_cell("—", col_w, "dim")
            lines.append(row)

        return "\n".join(lines)

    def _render_module_matrix(self, cols, col_w) -> str:
        lines = [self._col_header(cols, col_w, "MODULE (Lebensbereiche)")]
        for key, label in MODULE_LABELS.items():
            row = _pad_label(label)
            for stem, rec in cols:
                plain, color = _module_cell(rec.modules.get(key, {}))
                row += _pad_cell(plain, col_w, color)
            lines.append(row)
        return "\n".join(lines)

    def _render_coverage_matrix(self, cols, col_w) -> str:
        rows = [
            ("Versicherungssumme", lambda c: _short_versicherungssumme(c.get("versicherungssumme"))),
            ("Selbstbeteiligung", lambda c: _short_selbstbeteiligung(c.get("selbstbeteiligung"))),
            ("Wartezeit", lambda c: _short_wartezeit(c)),
            ("Geltungsbereich", lambda c: _short_geltungsbereich(c.get("geltungsbereich"))),
            ("Vertragslaufzeit", lambda c: _short_vertragslaufzeit(c.get("vertragslaufzeit"))),
        ]
        lines = [self._col_header(cols, col_w, "DECKUNG")]
        for label, fn in rows:
            row = _pad_label(label)
            for stem, rec in cols:
                row += _pad_cell(fn(rec.coverage or {}), col_w)
            lines.append(row)
        return "\n".join(lines)

    # Conservative partial-coverage cues: a matched item whose wording carries one
    # of these is shown as ~ rather than a flat ✓/✗ (the full wording is in the
    # subtext line and the [d] detail).
    _PARTIAL_CUES = ("nur ", "eingeschr", "außer", "ausser", "begrenzt", "teilweise")

    def _classify_columns(
        self, cols, kind: str, field: str
    ) -> tuple[list[dict[str, str]], list[list[str]], set[str]]:
        """Map each column's verbatim items into taxonomy categories. Returns
            (per_col_cat, per_col_sonst, present): per column the first verbatim hit
            per category, the unmatched 'Sonstige' bucket, and the union of matched
            category keys. Shared by the Vergleich matrix and the [t] full-text modal
            so the two never disagree on what matched what."""
        per_col_cat: list[dict[str, str]] = []
        per_col_sonst: list[list[str]] = []
        present: set[str] = set()
        for stem, rec in cols:
            catmap: dict[str, str] = {}
            sonst: list[str] = []
            for item in getattr(rec, field) or []:
                key = ctax.classify(item, kind)
                if key:
                    catmap.setdefault(key, item)
                    present.add(key)
                else:
                    sonst.append(item)
            per_col_cat.append(catmap)
            per_col_sonst.append(sonst)
        return per_col_cat, per_col_sonst, present

    def _render_category_matrix(self, kind: str, cols, col_w) -> str:
        title = "LEISTUNGEN (Vergleich)" if kind == "leistung" else "AUSSCHLÜSSE (Vergleich)"
        field = "leistungen" if kind == "leistung" else "ausschluesse"
        glyph, color = ("✓", "green") if kind == "leistung" else ("✗", "red")
        verbose = self._compare_verbose
        total_w = VERGLEICH_LABEL_W + len(cols) * col_w  # subtext width budget

        per_col_cat, per_col_sonst, present = self._classify_columns(cols, kind, field)
        ordered = [k for k in ctax.ordered_keys(kind) if k in present]
        lines = [self._col_header(cols, col_w, title)]

        for key in ordered:
            row = _pad_label(ctax.category_label(key))
            wordings: list[tuple[str, str]] = []
            for i, (stem, rec) in enumerate(cols):
                verbatim = per_col_cat[i].get(key)
                if verbatim is None:
                    row += _pad_cell("—", col_w, "dim")
                    continue
                low = verbatim.lower()
                if any(cue in low for cue in self._PARTIAL_CUES):
                    row += _pad_cell("~", col_w, "yellow")
                else:
                    row += _pad_cell(glyph, col_w, color)
                wordings.append((_col_label(stem), verbatim))
            lines.append(row)
            # Verbose only: each insurer's own wording on its OWN line, hard-
            # truncated so it never wraps into the next row. This is the naming
            # difference made visible (compact mode keeps just the glyph matrix).
            if verbose and wordings:
                for lbl, txt in wordings:
                    sub = _trunc(f"{lbl}: {txt}", total_w - 3)
                    lines.append(f"   [dim]{sub}[/dim]")

        total_sonst = sum(len(s) for s in per_col_sonst)
        if total_sonst:
            if verbose:
                for i, (stem, _rec) in enumerate(cols):
                    if per_col_sonst[i]:
                        full = _trunc(
                            f"… Sonstige ({_col_label(stem)}): "
                            + " · ".join(per_col_sonst[i]),
                            total_w - 3,
                        )
                        lines.append(f"   [dim]{full}[/dim]")
            sonst_hint = "" if verbose else " — \\[w] zeigt sie"
            lines.append(
                f"   [dim]({total_sonst} nicht zugeordnet{sonst_hint})[/dim]"
            )
        return "\n".join(lines)

    def _fulltext_entries(self) -> tuple[list[dict], int]:
        """Build the [t] full-text modal payload: one entry per taxonomy category
            present across the compared tariffs (Leistungen then Ausschlüsse), each
            with the verbatim wording per tariff, plus a per-category 'Sonstige'
            bucket. Uses the FULL column set (no width cap) — the modal is exactly
            where the tariffs that did not fit the matrix become readable. Returns
            (entries, n_cols)."""
        cols = self._coverage_columns()  # full set, intentionally not width-capped
        entries: list[dict] = []
        for kind, field, glyph, color, section in (
            ("leistung", "leistungen", "✓", "green", "Leistungen"),
            ("ausschluss", "ausschluesse", "✗", "red", "Ausschlüsse"),
        ):
            per_col_cat, per_col_sonst, present = self._classify_columns(
                cols, kind, field
            )
            for key in (k for k in ctax.ordered_keys(kind) if k in present):
                rows = [
                    (_col_label(stem), per_col_cat[i].get(key))
                    for i, (stem, _rec) in enumerate(cols)
                ]
                entries.append({
                    "section": section,
                    "glyph": glyph,
                    "color": color,
                    "label": ctax.category_label(key),
                    "rows": rows,
                })
            if any(per_col_sonst):
                rows = [
                    (_col_label(stem), " · ".join(per_col_sonst[i]) or None)
                    for i, (stem, _rec) in enumerate(cols)
                ]
                entries.append({
                    "section": section,
                    "glyph": "•",
                    "color": "yellow",
                    "label": "Sonstige (nicht zugeordnet)",
                    "rows": rows,
                })
        return entries, len(cols)

    def _render_snapshot_pricediff(self) -> str:
        """The legacy market-price drift across snapshots, appended only once a
            second snapshot exists (silently omitted otherwise)."""
        if len(self._all_snapshots) < 2:
            return ""
        _, old_path = self._all_snapshots[0]
        _, new_path = self._all_snapshots[-1]
        old_snap = load_snapshot(old_path)
        new_snap = load_snapshot(new_path)
        if old_snap is None or new_snap is None:
            return ""
        changes, added, removed = _compute_diff(old_snap, new_snap)
        lines = [
            f"[bold underline]Preisänderungen (Snapshots)[/bold underline]   "
            f"[dim]{old_snap.date} → {new_snap.date}[/dim]"
        ]
        for key, old_p, new_p, delta in sorted(changes, key=lambda x: x[3]):
            sign = "+" if delta > 0 else ""
            c = "bright_red" if delta > 0 else "bright_green"
            lines.append(f"  {_esc(key[:50].ljust(50))}  {old_p:.2f} → {new_p:.2f}  "
                         f"[{c}]{sign}{delta:.2f}[/{c}]")
        for k in added:
            lines.append(f"  [bright_green]+[/bright_green] {_esc(k)}")
        for k in removed:
            lines.append(f"  [bright_red]−[/bright_red] {_esc(k)}")
        if not (changes or added or removed):
            lines.append("[dim italic]keine Änderungen zwischen den Snapshots.[/dim italic]")
        return "\n".join(lines)

    # --- Verlauf dashboard ---

    def _verlauf_filter_rows(self, all_rows: list[dict]) -> list[dict]:
        filt = self._verlauf_filter
        if filt == "changed":
            return [
                r for r in all_rows
                if (r["delta_price"] is not None and r["delta_price"] != 0.0)
                or (r["delta_pos"] is not None and r["delta_pos"] != 0)
                or r["is_new"] or r["is_removed"]
            ]
        if filt == "cheaper":
            return [r for r in all_rows if r["delta_price"] is not None and r["delta_price"] < 0]
        if filt == "pricier":
            return [r for r in all_rows if r["delta_price"] is not None and r["delta_price"] > 0]
        return all_rows

    def _verlauf_market_line(self) -> str:
        """Second header line: market aggregates across ALL snapshots (count +
        median with sparkline). Empty string when fewer than 2 snapshots carry
        stats, so the header renders unchanged on thin data."""
        stats = load_market_stats()
        if len(stats) < 2:
            return ""
        medians = [s["median"] for s in stats]
        counts = [s["count"] for s in stats]
        first, last = stats[0], stats[-1]
        med_str = (
            f"{first['median']:.2f} → {last['median']:.2f} €"
            if first["median"] is not None and last["median"] is not None
            else "—"
        )
        cnt_str = (
            f"{counts[0]} → {counts[-1]}" if counts[0] != counts[-1] else f"{counts[-1]}"
        )
        return (
            f"\n[dim]Markt über Zeit:[/dim]  {len(stats)} Snapshots"
            f"  ·  Tarife {cnt_str} [cyan]{sparkline(counts)}[/cyan]"
            f"  ·  Median {med_str} [cyan]{sparkline(medians)}[/cyan]"
        )

    def _populate_verlauf(self) -> None:
        """Populate the Verlauf DataTable with price-drift data between two snapshots."""
        try:
            header: Label = self.query_one("#verlauf-header", Label)
            table: DataTable = self.query_one("#verlauf-table", DataTable)
        except NoMatches:
            return

        snaps = self._all_snapshots
        if len(snaps) < 2:
            header.update(
                "[dim italic]Mindestens 2 Snapshots erforderlich. "
                "Erneut scrapen, um einen zweiten zu erstellen.[/dim italic]"
            )
            table.clear(columns=True)
            return

        old_idx = max(0, min(self._verlauf_old_idx, len(snaps) - 2))
        old_date, old_path = snaps[old_idx]
        new_date, new_path = snaps[-1]
        old_snap = load_snapshot(old_path)
        new_snap = load_snapshot(new_path)
        if old_snap is None or new_snap is None:
            return

        n_snaps = len(snaps)
        snap_cycle = (
            f"  ·  [dim],[/dim]/[dim].[/dim] Snapshot ({old_idx + 1}/{n_snaps - 1})"
            if n_snaps > 2
            else ""
        )
        filter_lbl = _VERLAUF_FILTER_LABELS.get(self._verlauf_filter, "Alle")
        header.update(guard_content(
            f"[bold]Verlauf:[/bold]  {old_date} → {new_date}"
            f"  ·  [dim]Filter: [bold]{filter_lbl}[/bold]  ·  [bold]m[/bold] wechseln"
            f"  ·  [bold]d[/bold] Details{snap_cycle}[/dim]"
            + self._verlauf_market_line()
        ))

        rows = self._verlauf_filter_rows(_build_verlauf_rows(old_snap, new_snap))

        table.clear(columns=True)
        table.add_columns("#", "Anbieter", "Tarif", "SB", "Alt €/Mo", "Neu €/Mo", "Δ €", "Δ %", "Δ Rang")
        # Suffix the row key: r["key"] (insurer|product|SB) can repeat within a
        # snapshot (snapshot.py permits it), and add_row(key=…) would raise
        # DuplicateKey on the collision — the same guard the market table uses.
        self._verlauf_rows = {}
        self._verlauf_ident_to_rk = {}
        for i, r in enumerate(rows):
            row_key = f'{r["key"]}#{i}'
            table.add_row(*guard_cells(verlauf_row_cells(r)), key=row_key)
            self._verlauf_rows[row_key] = r
            self._verlauf_ident_to_rk.setdefault(r["key"], row_key)

    def _render_price_series(self, stem: str) -> str:
        """Preisverlauf section: the pinned-SB price series across all snapshots,
        as a sparkline plus the (last 8) dated points. Empty string below 2
        priced points — one price is a fact, not a Verlauf."""
        ci = self._change_summary.get(stem)
        if ci is None:
            return ""
        series = ci.price_series
        prices = [e.get("price") for e in series]
        priced = [p for p in prices if p is not None]
        if len(priced) < 2:
            return ""
        lo, hi = min(priced), max(priced)
        range_str = f"{lo:.2f}–{hi:.2f} €" if hi != lo else f"stabil {lo:.2f} €"
        shown = series[-8:]
        trunc = f"letzte {len(shown)} von {len(series)} · " if len(series) > len(shown) else ""
        pts = []
        for e in shown:
            p = f"{e['price']:.2f}" if e.get("price") is not None else "—"
            pts.append(f"[dim]{e['date'][5:]}[/dim] {p}")
        return (
            f"[bold underline]Preisverlauf[/bold underline]   "
            f"[dim]{len(series)} Snapshots · {trunc}gepinnte SB-Variante · {range_str}[/dim]\n"
            f"  [cyan]{sparkline(prices)}[/cyan]   " + "  ·  ".join(pts)
        )

    def _render_verlauf_detail(self, row: SnapshotRow) -> str:
        """Market detail + feature-diff section for the Verlauf tab."""
        base = self._render_market_detail(row)
        if not row.stem or not self._all_snapshots or len(self._all_snapshots) < 2:
            return base

        old_idx = max(0, min(self._verlauf_old_idx, len(self._all_snapshots) - 2))
        old_date = self._all_snapshots[old_idx][0]
        new_date = self._all_snapshots[-1][0]

        old_state, new_state, diff = load_feature_diff(row.stem, old_date, new_date)

        lines = []
        price_hist = self._render_price_series(row.stem)
        if price_hist:
            lines.extend(["", price_hist])
        lines += [
            "",
            f"[bold underline]Leistungsänderungen[/bold underline]   "
            f"[dim]{old_date} → {new_date}[/dim]",
        ]

        if old_state is None and new_state is None:
            lines.append("[dim italic]  nicht analysiert — keine Leistungs-Historie[/dim italic]")
            return base + "\n\n" + "\n".join(lines)

        if old_state is None:
            dt = new_state.get("_history_date", new_date)
            lines.append(f"  [cyan]erstmals analysiert am {dt}[/cyan]")
            return base + "\n\n" + "\n".join(lines)

        if not diff:
            lines.append("[dim italic]  keine Leistungsänderungen im gewählten Zeitraum[/dim italic]")
            return base + "\n\n" + "\n".join(lines)

        # --- modules ---
        if diff.get("modules"):
            lines.append("  [bold]Module[/bold]")
            for ch in diff["modules"]:
                lbl = MODULE_LABELS.get(ch["key"], ch["key"])
                oi, ni = ch["old_included"], ch["new_included"]
                ol, nl = ch["old_level"], ch["new_level"]
                if oi != ni:
                    if ni:
                        lines.append(f"    {lbl:<22} [bright_green]+ neu enthalten[/bright_green]")
                    else:
                        lines.append(f"    {lbl:<22} [bright_red]− nicht mehr enthalten[/bright_red]")
                elif ol != nl:
                    old_s = ol or "—"
                    new_s = nl or "—"
                    # Rank-based, not lexicographic: an unknown tier (or a same-rank
                    # casing-only change) stays neutral instead of being mislabelled
                    # an up-/downgrade.
                    direction = _level_direction(ol, nl)
                    col = ("bright_green" if direction == 1
                           else "bright_red" if direction == -1
                           else "cyan")
                    lines.append(f"    {lbl:<22} [{col}]{old_s} → {new_s}[/{col}]")

        # --- coverage ---
        if diff.get("coverage"):
            lines.append("  [bold]Deckung[/bold]")
            for ch in diff["coverage"]:
                old_v = _esc(str(ch["old"])) if ch["old"] is not None else "—"
                new_v = _esc(str(ch["new"])) if ch["new"] is not None else "—"
                lines.append(f"    {_esc(ch['field']):<22} [dim]{old_v}[/dim] → {new_v}")

        # --- leistungen ---
        if diff.get("leistungen"):
            lst = diff["leistungen"]
            if lst.get("added") or lst.get("removed"):
                lines.append("  [bold]Leistungen[/bold]")
                for item in lst.get("added", []):
                    lines.append(f"    [bright_green]+[/bright_green] {_esc(item)}")
                for item in lst.get("removed", []):
                    lines.append(f"    [bright_red]−[/bright_red] {_esc(item)}")

        # --- ausschluesse (reversed color: added=worse, removed=better) ---
        if diff.get("ausschluesse"):
            asl = diff["ausschluesse"]
            if asl.get("added") or asl.get("removed"):
                lines.append("  [bold]Ausschlüsse[/bold]")
                for item in asl.get("added", []):
                    lines.append(f"    [bright_red]+[/bright_red] {_esc(item)}  [dim](neu ausgeschlossen)[/dim]")
                for item in asl.get("removed", []):
                    lines.append(f"    [bright_green]−[/bright_green] {_esc(item)}  [dim](nicht mehr ausgeschlossen)[/dim]")

        # --- besonderheiten ---
        if diff.get("besonderheiten"):
            bsl = diff["besonderheiten"]
            if bsl.get("added") or bsl.get("removed"):
                lines.append("  [bold]Besonderheiten[/bold]")
                for item in bsl.get("added", []):
                    lines.append(f"    [cyan]+[/cyan] {_esc(item)}")
                for item in bsl.get("removed", []):
                    lines.append(f"    [dim]−[/dim] {_esc(item)}")

        return base + "\n\n" + "\n".join(lines)

    # --- Magic Find tab ---

    def _populate_magic(self) -> None:
        """Rank every analyzed tariff by combined quality (magic.rank) and fill the
            Magic-Find table, best first. Read-only over out/tariffs — PRICE IS SHOWN
            BUT NEVER SCORED. The deep-scan funnel (Phase 4) extends this with prescore
            candidates; here it ranks only what is already analyzed."""
        try:
            table: DataTable = self.query_one("#magic-table", DataTable)
            header: Label = self.query_one("#magic-header", Label)
        except NoMatches:
            return

        table.clear(columns=True)
        table.add_columns(
            "#", "Versicherer", "Tarif", "Score", "Note", "Bew.",
            "Mod", "Leist.", "Deckung", "Ext", "€/mo", "P/L",
        )
        self._magic_rows = {}
        self._magic_ident_to_rk = {}
        self._magic_snaprow_by_stem = {}
        self._magic_score_by_stem = {}

        if not self._snapshot:
            header.update("[dim italic]Kein Snapshot geladen.[/dim italic]")
            return

        self._magic_weights = magic.load_weights()
        needs = magic.load_needs() if self._magic_needs_mode else None
        reps = magic._representative_rows(self._snapshot.rows)
        self._magic_snaprow_by_stem = reps
        scores = magic.rank(self._snapshot.rows, self._details_by_stem,
                            self._magic_weights, needs=needs)

        # Coverage marker: how much of the distinct market we actually rank vs only
        # pre-score. Best price-efficiency in the field anchors the P/L column (display
        # only — quality_per_eur NEVER enters the score).
        n_market = len(magic.prescore(self._snapshot.rows))
        max_qpe = max((s.quality_per_eur() or 0.0 for s in scores), default=0.0)
        n_low_conf = sum(1 for s in scores if s.leistung_low_confidence)

        if self._magic_needs_mode:
            if needs is not None and magic.needs_are_neutral(needs):
                mode_note = (
                    "[bold yellow]🎯 Bedarf-Modus[/bold yellow] "
                    "[dim](neutral — config/needs-weights.json anpassen, damit es wirkt)"
                    "[/dim]"
                )
            else:
                mode_note = (
                    "[bold yellow]🎯 Bedarf-Modus[/bold yellow] "
                    "[dim]— Module nach deiner Gewichtung[/dim]"
                )
        else:
            mode_note = "[bold]✨ Magic Find[/bold] — objektive Marktqualität"
        cov_note = (
            f"[dim]{len(scores)}/{n_market} Markt-Produkte analysiert"
            + (f" · [yellow]{n_low_conf} mit dünner Extraktion ⚠[/yellow]"
               if n_low_conf else "")
            + " · \\[F] mehr scannen[/dim]"
        )
        # Structural blind spot: externally recommended direct sellers (WGV,
        # HUK-Coburg, …) never appear in the CHECK24 snapshot — name them here
        # instead of letting the ranking imply market-wide completeness.
        notes = external_market_notes(self._ext_ratings)
        blind_note = ""
        if notes:
            names = ", ".join(
                f"{n.get('versicherer', '?')} {n.get('tarif', '')}".strip()
                for n in notes
            )
            blind_note = (
                f"\n[yellow]⚠[/yellow] [dim]{len(notes)} extern empfohlene Tarife "
                f"außerhalb CHECK24 (Direktvertrieb): {_esc(names)}[/dim]"
            )
        header.update(guard_content(
            f"{mode_note}, [bold]Preis zählt nicht[/bold].  {cov_note}\n"
            f"[dim]↑↓ wählen · \\[P] Bedarf an/aus · \\[d] Score-Beitrag je Dimension · "
            "P/L = Preis-Leistung (nur Anzeige) · Ext = externe Tests (nur Anzeige)[/dim]"
            f"{blind_note}"
        ))

        for i, s in enumerate(scores):
            rank_no = i + 1
            marker = "[bright_green]▶[/bright_green]" if rank_no == 1 else str(rank_no)
            row_key = f"{s.stem}#{i}"
            self._magic_rows[row_key] = s
            self._magic_score_by_stem[s.stem] = s
            snaprow = reps.get(s.stem)

            if snaprow and snaprow.tarifnote:
                nc = _tarifnote_color(snaprow.tarifnote)
                note_col = f"[{nc}]{_esc(snaprow.tarifnote)}[/{nc}]"
            elif s.note is not None:
                note_col = f"{s.note:.1f}".replace(".", ",")
            else:
                note_col = "—"
            bew_cell = (
                _bewertung_cell(snaprow, *self._bew_lohi())
                if snaprow is not None else "[dim]—[/dim]"
            )
            price = f"{s.monatlich_eur:.0f}" if s.monatlich_eur is not None else "—"
            deckung = f"{s.dims.get('coverage_gen', 0.0) * 100:.0f}%"
            leist_cell = f"{s.n_leistung_cats}/24"
            if s.leistung_low_confidence:
                leist_cell = f"[yellow]{leist_cell} ⚠[/yellow]"
            qpe = s.quality_per_eur()
            if qpe is None or max_qpe <= 0:
                pl_cell = "[dim]—[/dim]"
            else:
                frac = qpe / max_qpe
                pl_cell = f"{frac * 100:3.0f}% {magic_bar(frac, 6)}"
            ext_cell = external_badge_cell(
                external_ratings_for(s.stem, s.insurer, self._ext_ratings))

            table.add_row(
                *guard_cells((
                    marker,
                    _esc(s.insurer),
                    _esc(s.product),
                    magic_score_cell(s.total),
                    note_col,
                    bew_cell,
                    f"{s.n_modules}/8",
                    leist_cell,
                    deckung,
                    ext_cell,
                    price,
                    pl_cell,
                )),
                key=row_key,
            )
            # Map both the stem and the representative row's bare key to this row, so a
            # tariff held in another tab can re-select here (best-effort; the SB band
            # may differ). First occurrence wins.
            self._magic_ident_to_rk.setdefault(s.stem, row_key)
            if snaprow and snaprow.key:
                self._magic_ident_to_rk.setdefault(snaprow.key, row_key)

    def _render_magic_detail(self, score: magic.MagicScore,
                             row: "SnapshotRow | None") -> str:
        """Score breakdown (per-dimension contribution) + the standard tariff detail."""
        weights = magic.load_weights().dim_weights()
        lines = [
            f"[bold]{_esc(score.insurer)}[/bold] — [italic]{_esc(score.product)}[/italic]",
            f"[bold underline]Magic-Score[/bold underline]  "
            f"{magic_score_cell(score.total, 16)}   "
            "[dim](Qualität; Preis fließt NICHT ein)[/dim]",
            "",
            "[bold underline]Score-Beitrag je Dimension[/bold underline]",
        ]
        for dim, raw in score.dims.items():
            label = MAGIC_DIM_LABELS.get(dim, dim)
            w = weights.get(dim, 0.0)
            contrib = score.contrib.get(dim, 0.0)
            lines.append(
                f"  {label:<24} {magic_bar(raw, 10)} {raw * 100:4.0f}%   "
                f"[dim]×{w:.2f} = {contrib:.3f}[/dim]"
            )

        # Info-only signals — explicitly NOT part of the weighted score above.
        lines.append("")
        lines.append("[bold underline]Zusatz-Info (nicht gewertet)[/bold underline]")
        lines.append(
            f"  {'Modul-Stufe':<24} {magic_bar(score.module_tier_raw, 10)} "
            f"{score.module_tier_raw * 100:4.0f}%   "
            "[dim](Basis/Komfort/Premium — meist nicht extrahiert, daher nicht gewertet)"
            "[/dim]"
        )
        if score.monatlich_eur and score.quality_per_eur() is not None:
            lines.append(
                f"  {'Preis-Leistung':<24} [dim]{score.total:.3f} Qualität / "
                f"{score.monatlich_eur:.0f}€/Monat — reiner Anzeigewert, kein "
                "Score-Faktor[/dim]"
            )
        if score.leistung_low_confidence:
            lines.append(
                f"  [yellow]⚠ Leistungs-Extraktion dünn[/yellow] [dim]— nur "
                f"{score.n_leistung_cats} Kategorien erkannt; der Leistungs-Score ist "
                "evtl. zu niedrig (Recall-Lücke, kein armer Tarif). \\[F] / neu "
                "extrahieren hilft.[/dim]"
            )

        lines.append("")
        if row is not None:
            lines.append(self._render_market_detail(row))
        else:
            lines.append(
                "[dim italic]Kein passender Snapshot-Tarif — nur der Score (Detail "
                "nicht verfügbar).[/dim italic]"
            )
        return "\n".join(lines)

    def _refresh_magic_detail(self) -> None:
        """Re-render the Magic band live — only when it is currently shown."""
        try:
            band = self.query_one("#magic-detail")
        except NoMatches:
            return
        if band.display:
            self._render_active_into(("#magic-detail", "#magic-detail-content"))

    # --- Event handlers ---

    def _move_cursor_safe(self, table_id: str, rk: str) -> None:
        """Move a table's cursor to row-key rk if not already there. Silent only on
        the row genuinely not being in this table (the held tariff isn't here) — any
        other error surfaces instead of being swallowed."""
        try:
            table = self.query_one(table_id, DataTable)
        except NoMatches:
            return
        try:
            row_idx = table.get_row_index(rk)
        except RowDoesNotExist:
            return
        if table.cursor_coordinate.row != row_idx:
            table.move_cursor(row=row_idx)

    def _select_held(self, active: str) -> str | None:
        """Best-effort cursor move: if the held tariff is present in the newly-active
        table, move the cursor onto it; otherwise leave the cursor as-is. This does
        NOT set the active-selection state — _adopt_cursor_row does that afterwards,
        from wherever the cursor actually ends up, so a held tariff absent from this
        tab can't leave the state pointing at the previous tab's row. Returns the
        table id when it moved onto the held row, else None."""
        ident = self._held_ident
        if not ident:
            return None
        spec = TAB_SPECS.get(active)
        if spec is None or spec.ident_map is None:
            return None
        ident_to_rk = getattr(self, spec.ident_map)
        rows = getattr(self, spec.rows_map)
        rk = ident_to_rk.get(ident)
        if rk and rk in rows:
            self._move_cursor_safe(spec.table_id, rk)
            return spec.table_id
        return None

    def _adopt_cursor_row(self, active: str) -> None:
        """Reconcile _active_row/_active_fav/_held_ident to the row the cursor is
        PHYSICALLY on in the active table — the single source of active-selection
        truth after a tab switch or reload. Without this, a held tariff that isn't
        in the new tab would leave [u]/[R]/[D] operating on the previous tab's row."""
        table = self._active_data_table()
        if table is None or table.row_count == 0:
            return
        try:
            rk = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except CellDoesNotExist:
            return
        if rk is None:
            return
        spec = TAB_SPECS.get(active)
        if spec is None or spec.adopt is None:
            return
        # Dispatch to the per-tab reconciler. These _adopt_* helpers are called ONLY
        # from here, so this method stays the single reconcile entry point (the
        # cross-tab SOLE-writer invariant) — the split is just per-tab unpacking.
        getattr(self, spec.adopt)(str(rk))

    def _adopt_favorites(self, rk: str) -> None:
        entry = self._fav_rows.get(rk)
        if entry is not None:
            row, fav = entry
            self._active_row, self._active_fav = row, fav
            self._held_ident = self._tariff_key(row, fav)

    def _adopt_market(self, rk: str) -> None:
        row = self._market_rows.get(rk)
        if row is not None:
            self._active_row, self._active_fav = row, None
            self._held_ident = row.key or self._held_ident

    def _adopt_verlauf(self, rk: str) -> None:
        rdict = self._verlauf_rows.get(rk)
        if rdict is not None:
            ident = rdict["key"]
            self._active_row = (
                next((r for r in self._snapshot.rows if r.key == ident), None)
                if self._snapshot else None
            )
            self._active_fav = None
            self._held_ident = ident

    def _adopt_magic(self, rk: str) -> None:
        score = self._magic_rows.get(rk)
        if score is not None:
            row = self._magic_snaprow_by_stem.get(score.stem)
            self._active_row = row  # may be None (detail-only stem)
            self._active_fav = None
            self._held_ident = (row.key if (row and row.key) else None) or score.stem

    @on(DataTable.RowHighlighted, "#market-table")
    def on_market_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """A single click / arrow move highlights a row — track it as the active
            target and show/refresh the detail band when _detail_visible is set."""
        key = str(event.row_key.value) if event.row_key.value is not None else None
        if not self._snapshot or key is None:
            return
        row = self._market_rows.get(key)
        if row is None:
            return
        try:
            active = self.query_one("#tabs", TabbedContent).active
        except NoMatches:
            active = None
        if active == "market":
            self._active_row = row
            self._active_fav = None
            if row.key:
                self._held_ident = row.key
            if self._detail_visible:
                self._show_detail()
            else:
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
        try:
            active = self.query_one("#tabs", TabbedContent).active
        except NoMatches:
            active = None
        if active == "favorites":
            self._active_row = row  # may be None (favorite not in snapshot)
            self._active_fav = fav
            ident = self._tariff_key(row, fav)
            if ident:
                self._held_ident = ident
            if self._detail_visible:
                self._show_detail()
            else:
                self._refresh_fav_detail()

    @on(DataTable.RowHighlighted, "#verlauf-table")
    def on_verlauf_highlighted(self, event: DataTable.RowHighlighted) -> None:
        key = str(event.row_key.value) if event.row_key.value is not None else None
        if not key or not self._snapshot:
            return
        rdict = self._verlauf_rows.get(key)  # key is the suffixed row key
        if rdict is None:
            return
        ident = rdict["key"]
        row = next((r for r in self._snapshot.rows if r.key == ident), None)
        if row is None:
            return  # removed tariff (only in the old snapshot) — no live detail
        try:
            active = self.query_one("#tabs", TabbedContent).active
        except NoMatches:
            active = None
        if active == "verlauf":
            self._active_row = row
            self._active_fav = None
            self._held_ident = ident
            if self._detail_visible:
                try:
                    content = self.query_one("#verlauf-detail-content", Static)
                    content.update(guard_content(self._render_verlauf_detail(row)))
                except NoMatches:
                    pass

    @on(DataTable.RowSelected, "#verlauf-table")
    def on_verlauf_selected(self, event: DataTable.RowSelected) -> None:
        self.on_verlauf_highlighted(event)
        self._detail_visible = True
        try:
            band = self.query_one("#verlauf-detail")
            band.display = True
            content = self.query_one("#verlauf-detail-content", Static)
            if self._active_row:
                content.update(guard_content(
                    self._render_verlauf_detail(self._active_row)))
            band.scroll_home(animate=False)
            band.focus()
        except NoMatches:
            pass

    # Enter / double-click: open + focus the detail band. Deliberately NO browser
    # launch here — that used to be a surprising side effect that contradicted the
    # in-app hint ("↵ Detail"); the offer opens explicitly via [O] or the ↗ link.
    @on(DataTable.RowSelected, "#market-table")
    def on_market_selected(self, event: DataTable.RowSelected) -> None:
        self.on_market_highlighted(event)  # ensure active row is current
        self._detail_visible = True
        self._show_detail(focus=True)

    @on(DataTable.RowSelected, "#fav-table")
    def on_fav_selected(self, event: DataTable.RowSelected) -> None:
        self.on_fav_highlighted(event)
        self._detail_visible = True
        self._show_detail(focus=True)

    @on(DataTable.RowHighlighted, "#magic-table")
    def on_magic_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Track the highlighted Magic row as the active tariff (via its
            representative snapshot row) and refresh the score-breakdown band."""
        key = str(event.row_key.value) if event.row_key.value is not None else None
        if key is None:
            return
        score = self._magic_rows.get(key)
        if score is None:
            return
        try:
            active = self.query_one("#tabs", TabbedContent).active
        except NoMatches:
            active = None
        if active == "magic":
            row = self._magic_snaprow_by_stem.get(score.stem)
            self._active_row = row
            self._active_fav = None
            self._held_ident = (row.key if (row and row.key) else None) or score.stem
            if self._detail_visible:
                self._show_detail()
            else:
                self._refresh_magic_detail()

    @on(DataTable.RowSelected, "#magic-table")
    def on_magic_selected(self, event: DataTable.RowSelected) -> None:
        self.on_magic_highlighted(event)
        self._detail_visible = True
        self._show_detail(focus=True)

    @on(DataTable.HeaderSelected, "#market-table")
    def on_header_selected(self, event: DataTable.HeaderSelected) -> None:
        col_map = {0: "position", 2: "insurer", 4: "note", 6: "price"}
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
        if self._filter_timer is not None:
            self._filter_timer.stop()
        self._filter_timer = self.set_timer(0.15, self._populate_market_table)

    @on(Input.Submitted, "#filter-input")
    def on_filter_submitted(self, event: Input.Submitted) -> None:
        """Enter in the filter keeps the filter text and returns to the table —
        without this the only way out was Esc, which also CLEARS the filter."""
        try:
            self.query_one("#market-table", DataTable).focus()
        except NoMatches:
            pass

    def on_resize(self, event) -> None:
        """Re-render the Vergleich matrix on resize so its width-capped columns
            track the new terminal size instead of leaving a stale layout."""
        self._populate_coverage()

    # --- Actions ---

    def action_focus_filter(self) -> None:
        try:
            self.query_one("#filter-input", Input).focus()
        except NoMatches:
            pass

    # Band ids for the Esc dismiss check (focus sits on the ScrollableContainer).
    _BAND_IDS = frozenset(
        s.band_id.lstrip("#") for s in TAB_SPECS.values() if s.band_id
    )

    def action_clear_filter(self) -> None:
        """Esc, contextual dismiss: a focused detail band (Enter/[d] moved focus
        there for scrolling) hands focus back to the active table; on the Market
        tab it clears the filter. Previously this always reached for the Market
        widgets — which focus-stole the *hidden* market table from other tabs."""
        focused = self.focused
        if focused is not None and focused.id in self._BAND_IDS:
            table = self._active_data_table()
            if table is not None:
                table.focus()
                return
        try:
            active = self.query_one("#tabs", TabbedContent).active
        except NoMatches:
            return
        if active != "market":
            return
        try:
            inp = self.query_one("#filter-input", Input)
            inp.value = ""
            self.filter_text = ""
            self.query_one("#market-table", DataTable).focus()
        except NoMatches:
            pass

    def _apply_sort(self, col: str, default_asc: bool = True) -> None:
        """Shared sort action: scoped to the Market tab (a sort key pressed on any
        other tab used to re-order the invisible market table silently), with a
        repeated press of the same key flipping the direction — the header arrow
        set in _populate_market_table makes both visible."""
        try:
            active = self.query_one("#tabs", TabbedContent).active
        except NoMatches:
            return
        if active != "market":
            self.notify("Sortierung gilt im Markt-Tab \\[x].",
                        severity="information", timeout=3)
            return
        if self.sort_col == col:
            self.sort_asc = not self.sort_asc
        else:
            self.sort_col = col
            self.sort_asc = default_asc
        self._populate_market_table()

    def action_sort_price(self) -> None:
        self._apply_sort("price")

    def action_sort_note(self) -> None:
        self._apply_sort("note")

    def action_sort_position(self) -> None:
        self._apply_sort("position")

    def action_sort_changed(self) -> None:
        self._apply_sort("changed", default_asc=False)  # most-recently changed first

    def action_switch_tab(self, tab_id: str) -> None:
        try:
            tabs = self.query_one("#tabs", TabbedContent)
            tabs.active = tab_id
        except NoMatches:
            pass

    # Tab order for cycling; "diff" (Vergleich) and "bench" have no table but are
    # still stops.
    _TAB_ORDER = ["favorites", "market", "diff", "verlauf", "bench", "magic"]

    def action_cycle_tab(self, delta: int) -> None:
        """Cycle the active tab forward (Tab) / backward (Shift+Tab). While a modal
        is open or a text field is focused, fall back to Textual's default focus
        navigation so Tab keeps moving between fields there."""
        if len(self.screen_stack) > 1 or isinstance(self.focused, (Input, TextArea)):
            if delta > 0:
                self.screen.focus_next()
            else:
                self.screen.focus_previous()
            return
        try:
            tabs = self.query_one("#tabs", TabbedContent)
        except NoMatches:
            return
        if tabs.active not in self._TAB_ORDER:
            return
        idx = (self._TAB_ORDER.index(tabs.active) + delta) % len(self._TAB_ORDER)
        tabs.active = self._TAB_ORDER[idx]

    def _center_cursor(self, table: DataTable) -> None:
        """Scroll so the cursor row sits in the vertical middle of the viewport.
        Used after a tab switch and after toggling the detail band, so the
        selection is never left off-screen or hidden behind the band."""
        try:
            region = table._get_cell_region(table.cursor_coordinate)
            table.scroll_to_region(region, animate=False, center=True, immediate=True)
        except Exception:
            pass

    def _center_cursor_in(self, table_id: str) -> None:
        try:
            table = self.query_one(table_id, DataTable)
        except NoMatches:
            return
        self._center_cursor(table)

    def _active_data_table(self) -> "DataTable | None":
        try:
            active = self.query_one("#tabs", TabbedContent).active
        except NoMatches:
            return None
        spec = TAB_SPECS.get(active)
        if spec is None or spec.table_id is None:
            return None
        try:
            return self.query_one(spec.table_id, DataTable)
        except NoMatches:
            return None

    def _center_active_cursor(self) -> None:
        table = self._active_data_table()
        if table is not None:
            self._center_cursor(table)

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        """Move focus to the activated tab's primary scrollable widget. Without
            this, Textual focuses the first focusable widget in the new pane — on the
            Market tab that is the dock-top #filter-input, which then swallows the
            global single-letter shortcuts (y/x/v/…) as filter text instead of
            switching tabs. The filter stays reachable with [f].

            The Vergleich tab MUST be in this map too: otherwise switching to it left
            focus stranded on the now-hidden source table, so the next tab shortcut
            (and arrow-scrolling the diff) did not register — the 'x does not switch
            from Favorites' bug. Every tab now lands focus on a stable, non-Input
            widget of its own pane."""
        try:
            active = self.query_one("#tabs", TabbedContent).active
        except NoMatches:
            return
        spec = TAB_SPECS.get(active)
        if spec is None:
            return
        try:
            self.query_one(spec.focus_id).focus()
        except NoMatches:
            pass
        # Move the cursor to the held tariff if it is in this tab, then reconcile the
        # active selection to wherever the cursor actually is (held-absent included)
        # so tab-specific actions ([u]/[R]/[N]/[D]) never target the previous tab's
        # row. Center afterwards, once the pane's layout has settled.
        if active in ROW_TABS:
            self._select_held(active)
            self._adopt_cursor_row(active)
            self.call_after_refresh(self._center_cursor_in, spec.focus_id)

    def _reload_all(self) -> None:
        """Reload every data source from disk and repaint both tables, the header
            and whichever detail band is shown. Used after [g], a favorite edit or a
            delete so the UI reflects the new on-disk state."""
        self._load_data()
        self._populate_favorites_table()
        self._populate_market_table()
        # Rebuild the Vergleich matrix here (not per market-filter keystroke): its
        # inputs are the compare set and the analyzed records, both of which a reload
        # can change — the market filter/sort cannot.
        self._populate_coverage()
        self._populate_verlauf()
        self._populate_benchmark()
        self._populate_magic()
        self._update_header()
        # clear() reset every cursor to row 0; move it back onto the held tariff in
        # the active tab and reconcile the active state, so a follow-up [u]/[R]/[D]
        # still targets the same tariff instead of whatever landed at row 0.
        try:
            active = self.query_one("#tabs", TabbedContent).active
        except NoMatches:
            active = None
        if active in ROW_TABS:
            self._select_held(active)
            self._adopt_cursor_row(active)
        self._refresh_market_detail()
        self._refresh_fav_detail()
        self._refresh_verlauf_detail()
        self._refresh_magic_detail()
        self._update_status_bar()

    def action_refresh_data(self) -> None:
        self._reload_all()
        self.notify("Daten neu geladen.", timeout=3)

    def _verlauf_tab_active(self) -> bool:
        """Scope guard for the Verlauf-only keys ([m], [,], [.]): pressed anywhere
        else they used to mutate the invisible Verlauf state silently."""
        try:
            return self.query_one("#tabs", TabbedContent).active == "verlauf"
        except NoMatches:
            return False

    def action_verlauf_filter(self) -> None:
        if not self._verlauf_tab_active():
            self.notify("Verlauf-Filter gilt im Verlauf-Tab \\[l].",
                        severity="information", timeout=3)
            return
        idx = _VERLAUF_FILTERS.index(self._verlauf_filter) if self._verlauf_filter in _VERLAUF_FILTERS else 0
        self._verlauf_filter = _VERLAUF_FILTERS[(idx + 1) % len(_VERLAUF_FILTERS)]
        self._populate_verlauf()
        lbl = _VERLAUF_FILTER_LABELS.get(self._verlauf_filter, "")
        self.notify(f"Verlauf-Filter: {lbl}", timeout=2)

    def action_verlauf_prev_snap(self) -> None:
        if not self._verlauf_tab_active() or len(self._all_snapshots) < 3:
            return
        self._verlauf_old_idx = max(0, self._verlauf_old_idx - 1)
        self._populate_verlauf()

    def action_verlauf_next_snap(self) -> None:
        max_idx = len(self._all_snapshots) - 2
        if not self._verlauf_tab_active() or max_idx <= 0:
            return
        self._verlauf_old_idx = min(max_idx, self._verlauf_old_idx + 1)
        self._populate_verlauf()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    _THEMES = [
        "textual-dark", "rose-pine", "rose-pine-moon", "rose-pine-dawn",
        "nord", "gruvbox", "dracula", "tokyo-night", "catppuccin-mocha",
        "textual-light",
    ]

    def action_next_theme(self) -> None:
        try:
            idx = self._THEMES.index(self.theme)
        except ValueError:
            idx = -1
        self.theme = self._THEMES[(idx + 1) % len(self._THEMES)]
        self._save_prefs()
        self.notify(f"Theme: {self.theme} (gespeichert)", timeout=2)

    # --- Build the CHECK24 result URL ([b]) ---

    def _load_query_module(self):
        """Import scripts/check24_query.py as a module, or notify + return None.

            tui.py keeps check24_query as the single owner of the lever vocabulary and
            parse/rebuild logic; both [b] (build URL) and [e] (edit) load it this way."""
        import importlib.util

        qpath = REPO_ROOT / "scripts" / "check24_query.py"
        try:
            spec = importlib.util.spec_from_file_location("check24_query", qpath)
            cq = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cq)
            return cq
        except Exception as exc:  # noqa: BLE001 — surface any load failure to the user
            self.notify(f"check24_query.py nicht ladbar: {exc}", severity="error", timeout=6)
            return None

    def _load_query_profile(self):
        """Return (profile_dict, base, query, is_example) or None (after notify).

            Prefers the real (gitignored) profile and falls back to the tracked example
            with is_example=True, mirroring check24_query.load_profile()."""
        ppath = REPO_ROOT / "config" / "check24-profile.json"
        epath = REPO_ROOT / "config" / "check24-profile.example.json"
        is_example = False
        try:
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
                return None
        except (json.JSONDecodeError, OSError) as exc:
            self.notify(f"Query-Profil unlesbar: {exc}", severity="error", timeout=6)
            return None

        base = profile.get("base_url") if isinstance(profile, dict) else None
        query = profile.get("query") if isinstance(profile, dict) else None
        if not base or not isinstance(query, str):
            self.notify("Profil ohne base_url/query (string).", severity="error", timeout=6)
            return None
        return profile, base, query, is_example

    def action_build_query(self) -> None:
        """Rebuild the CHECK24 result URL(s) from the saved profile and write them
            to tmp/ for the manual browser + scrape workflow (no headless path — bot
            gating). Reuses scripts/check24_query.py for the lever decode."""
        import contextlib
        import io
        from urllib.parse import parse_qsl, urlencode

        cq = self._load_query_module()
        if cq is None:
            return
        loaded = self._load_query_profile()
        if loaded is None:
            return
        _profile, base, query, is_example = loaded

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

    # Curated, editable lever keys (the set QueryEditScreen exposes). discounts is
    # intentionally NOT here (read-only JSON blob); every OTHER query param is
    # preserved verbatim by routing only these through set_param.
    _EDIT_LEVER_KEYS = (
        "provider_filter", "tariff_position", "maritalstatus", "birthdate",
        "zipcode", "employmentstatus", "employmentstatus_partner", "costsharing",
        "sortfield", "sortorder",
        "module_priv", "module_job", "module_traffic", "module_living",
        "module_rental", "stiftung_warentest",
    )

    def action_edit_query(self) -> None:
        """Open an interactive editor for the curated CHECK24 query levers, then
            (on confirm) write the rebuilt query back to config/check24-profile.json
            atomically and show the resulting URL. Only the touched levers are
            overridden via check24_query.set_param; every other query param (repeated
            keys, uncurated/unknown params) survives the round-trip verbatim."""
        from urllib.parse import parse_qsl, urlencode

        cq = self._load_query_module()
        if cq is None:
            return
        loaded = self._load_query_profile()
        if loaded is None:
            return
        profile, base, query, is_example = loaded

        pairs = parse_qsl(query, keep_blank_values=True)
        # last-wins matches browser semantics for repeated keys; this is the
        # pre-fill value shown in each field.
        current = dict(pairs)
        values = {k: current.get(k, "") for k in self._EDIT_LEVER_KEYS}
        discounts = cq.decode_discounts(pairs)

        def _on_edit(result: dict[str, str] | None) -> None:
            if result is None:
                return  # cancelled
            # Apply ONLY the levers the user actually changed; preserve every
            # other param (order, encoding, repeated/uncurated keys) verbatim.
            # set_param removes-all-then-appends, so routing an UNCHANGED key
            # through it would needlessly move it to the tail and reshuffle the
            # whole query on every save.
            new_pairs = list(pairs)
            changes: list[tuple[str, str, str]] = []
            for key in self._EDIT_LEVER_KEYS:
                new_val = result.get(key, "")
                old_val = values.get(key, "")
                if new_val == old_val:
                    continue  # untouched: leave the original pair (or absence)
                # A key absent from the source query (old_val == "") left at the
                # switch-off default ("no") is not a user change — injecting
                # key=no would flip an implicit server default into an explicit
                # exclusion. (Empty text fields hit the new_val == old_val skip.)
                if old_val == "" and new_val == "no":
                    continue
                changes.append((key, old_val, new_val))
                new_pairs = cq.set_param(new_pairs, key, new_val)

            def _on_confirm(ok: bool | None) -> None:
                if not ok:
                    return
                # No real lever change -> keep the source query byte-for-byte
                # (don't re-encode), so a confirmed "Query bleibt gleich" save is
                # a true no-op instead of a silent re-encode of untouched params.
                new_query = urlencode(new_pairs) if changes else query
                out_profile = dict(profile)
                out_profile["query"] = new_query
                ppath = REPO_ROOT / "config" / "check24-profile.json"
                try:
                    # Atomic write (temp twin on the same dir + os.replace), like
                    # snapshot.build() / _save_favorites: a crash mid-write must not
                    # truncate the only copy of the real (PII-bearing) profile.
                    tmp = ppath.with_suffix(".json.tmp")
                    tmp.write_text(
                        json.dumps(out_profile, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
                    os.replace(tmp, ppath)
                except OSError as exc:
                    self.notify(
                        f"Speichern fehlgeschlagen ({ppath.name}): {exc}",
                        severity="error",
                        timeout=8,
                    )
                    return
                # The query just changed on disk; drop the cached offer-URL base so
                # CHECK24 links rebuild from the new params (not the next [r] reload).
                if hasattr(self, "_offer_url_cache"):
                    del self._offer_url_cache
                # Show the resulting URL so the user can paste it into the browser.
                # is_example is now False — we just wrote the real profile.
                new_url = base + "?" + new_query
                out = REPO_ROOT / "tmp" / "check24-query.txt"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(f"# saved query\n{new_url}\n", encoding="utf-8")
                self.notify("Suche gespeichert (config/check24-profile.json).",
                            severity="information", timeout=5)
                levers_lines = [f"  {k}: {result.get(k, '')}" for k in self._EDIT_LEVER_KEYS]
                self.push_screen(QueryUrlScreen(
                    "\n".join(levers_lines), str(out.relative_to(REPO_ROOT)), False))

            self.push_screen(
                QuerySaveConfirmScreen(changes, is_example), _on_confirm)

        self.push_screen(
            QueryEditScreen(values, cq.provider_name, discounts, is_example),
            _on_edit,
        )

    # --- Favorites management ([u] toggle, [D] delete) ---

    def _save_favorites(self) -> None:
        """Persist the (tracked, PII-free) shortlist back to config/favorites.json.

            Atomic write: this is the only copy of a hand-curated file, and a crash /
            full disk mid-write would truncate it (load_favorites then silently returns
            {} — the shortlist would vanish). Write a temp file, then os.replace()."""
        path = REPO_ROOT / "config" / "favorites.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self._favorites, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)

    def _save_favorite_notes(self) -> None:
        """Persist the per-stem notes to the gitignored sidecar config/favorite-notes.json
        (atomic). Kept out of favorites.json so a typed personal note is never tracked."""
        atomic_write_json(
            REPO_ROOT / "config" / "favorite-notes.json", self._favorite_notes
        )

    def _migrate_favorite_notes(self) -> None:
        """One-time code migration: move any legacy inline `note` fields out of the
        tracked favorites.json into the sidecar, then rewrite favorites.json without
        them (all other keys byte-identical). Mirrors the compare_stems seed/persist
        pattern. favorites.json currently carries no notes, so this is a no-op in
        practice — but it must exist so an older tracked file can never leak a note."""
        favs = self._favorites.get("favorites", [])
        moved = False
        for f in favs:
            note = f.pop("note", None)  # drop from the tracked structure regardless
            if note is None:
                continue
            moved = True
            stem = f.get("stem")
            if stem:  # keyed by stem; don't clobber a note already in the sidecar
                self._favorite_notes.setdefault(stem, str(note))
        if moved:
            self._save_favorite_notes()
            self._save_favorites()  # favorites.json rewritten sans note fields

    def _is_favorite_stem(self, stem: str | None) -> bool:
        if not stem:
            return False
        return any(f.get("stem") == stem for f in self._favorites.get("favorites", []))

    def _row_tab_active(self) -> bool:
        """True only on a tab that shows a selectable row (favorites/market/verlauf).
        The table-less diff/bench tabs carry no row, yet _active_row/_active_fav keep
        the LAST table tab's selection (the tab-switch reconcile skips table-less tabs).
        Row-cursor actions ([u]/[R]/[a]/[N]/[o]/[D]/[g]/[G]/[H]) must consult this so they
        never fire on that stale, now-invisible row — [D] rmtrees it, [g]/[H] pay for it."""
        try:
            return self.query_one("#tabs", TabbedContent).active in ROW_TABS
        except NoMatches:
            return False

    def _active_identity(self) -> tuple[str, str, str | None] | None:
        """(insurer, product, stem) of the active favorite or market row, or None.
        None on a table-less tab, so the actions' existing "Erst eine Zeile wählen"
        guard fires instead of targeting the previous tab's invisible selection."""
        if not self._row_tab_active():
            return None
        if self._active_fav is not None:
            f = self._active_fav
            return f.get("insurer", ""), f.get("product", ""), f.get("stem")
        if self._active_row is not None:
            r = self._active_row
            return r.insurer, r.product, r.stem
        return None

    def _snapshot_row_for(
        self, stem: str, insurer: str, product: str
    ) -> SnapshotRow | None:
        """The snapshot row backing an identity — the active market row if set,
            else looked up by stem (then insurer+product) in the current snapshot.
            Needed to drive the analyze pipeline when [a] is pressed on a favorite
            (where _active_row is None) rather than a market row."""
        if self._active_row is not None:
            return self._active_row
        if self._snapshot is None:
            return None
        for r in self._snapshot.rows:
            if stem and r.stem == stem:
                return r
        for r in self._snapshot.rows:
            if r.insurer == insurer and r.product == product:
                return r
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
            self.notify(f"Aus Favoriten entfernt: {_esc(insurer)} {_esc(product)}", timeout=4)
        else:
            entry: dict[str, Any] = {"insurer": insurer, "product": product}
            if stem:
                entry["stem"] = stem
            if self._active_row is not None and self._active_row.selbstbeteiligung:
                entry["show_sb"] = self._active_row.selbstbeteiligung
            entry["tag"] = "in TUI hinzugefügt"
            favs.append(entry)
            self._save_favorites()
            self.notify(f"Zu Favoriten hinzugefügt: {_esc(insurer)} {_esc(product)}", timeout=4)
        self._reload_all()

    def action_edit_note(self) -> None:
        """Edit the free-text note on the active favorite — per-favorite context the
            dashboard shows, saved to the gitignored config/favorite-notes.json sidecar
            (keyed by stem), NOT the tracked favorites.json. Only favorites carry notes;
            on a non-favorite row it points to \\[u]."""
        ident = self._active_identity()
        if ident is None:
            self.notify("Erst einen Favoriten wählen (Pfeile / Klick).",
                        severity="warning")
            return
        insurer, product, stem = ident
        if not stem:
            self.notify(
                "Notiz braucht einen kanonischen stem (Tarif ohne Manifest-Eintrag).",
                severity="warning",
                timeout=6,
            )
            return
        if not self._is_favorite_stem(stem):
            self.notify(
                f"{_esc(insurer)} {_esc(product)} ist kein Favorit — erst \\[u] hinzufügen.",
                severity="warning",
                timeout=6,
            )
            return

        def _save(note: str | None) -> None:
            if note is None:
                return  # cancelled
            note = note.strip()
            if note:
                self._favorite_notes[stem] = note
            else:
                self._favorite_notes.pop(stem, None)  # empty text clears the note
            self._save_favorite_notes()
            self.notify(
                ("Notiz gespeichert" if note else "Notiz entfernt")
                + f": {_esc(insurer)} {_esc(product)}",
                timeout=4,
            )
            self._reload_all()

        self.push_screen(
            NoteEditScreen(f"{insurer} {product}", self._favorite_notes.get(stem, "")),
            _save,
        )

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
            f"Referenz: {_esc(insurer)} {_esc(product)} (SB {_esc(sb) or '—'}) — Δ neu berechnet.",
            timeout=5,
        )
        self._reload_all()

    def action_add_to_compare(self) -> None:
        """Add (or remove) the active tariff to the curated Vergleich. The
            comparison and the favorites star are separate lists — this touches only
            compare_stems. A new member that isn't analyzed yet is queued; if its
            source URLs are harvested it is analyzed in the background after a confirm
            (extract is a paid model call), so its column appears automatically."""
        ident = self._active_identity()
        if ident is None:
            self.notify("Erst eine Zeile wählen (Pfeile / Klick).", severity="warning")
            return
        insurer, product, stem = ident
        if not stem:
            stem = resolve_stem(insurer, product)
        if not stem:
            self.notify(
                "Vergleich braucht einen kanonischen stem (Tarif ohne "
                "Manifest-Eintrag).",
                severity="warning",
                timeout=6,
            )
            return

        current = self._compare_stems()
        if stem in current:
            current.remove(stem)
            self._set_compare_stems(current)
            self.notify(f"Aus Vergleich entfernt: {_esc(insurer)} {_esc(product)}", timeout=4)
            self._reload_all()
            return

        current.append(stem)
        self._set_compare_stems(current)
        if stem in self._details_by_stem:
            self.notify(f"Zum Vergleich hinzugefügt: {_esc(insurer)} {_esc(product)}", timeout=4)
            self._reload_all()
            return

        # Queued but not analyzed yet — analyze it so the column can appear.
        if self._pipeline_running:
            self.notify(
                f"Zum Vergleich vorgemerkt — eine Analyse läuft, \\[g] startet "
                f"{_esc(insurer)} {_esc(product)} danach.",
                timeout=6,
            )
            self._reload_all()
            return
        row = self._snapshot_row_for(stem, insurer, product)
        entry = self._doc_entry(row) if row is not None else None
        if row is not None and entry and entry.get("docs"):
            def _go(confirmed: bool | None) -> None:
                if confirmed and self._claim_pipeline():
                    self._run_pipeline(entry, row)
                else:
                    self.notify(
                        f"Im Vergleich vorgemerkt — \\[g] startet die Analyse für "
                        f"{_esc(insurer)} {_esc(product)} später.",
                        timeout=6,
                    )
                self._reload_all()

            self.push_screen(ConfirmFetchScreen(entry, ANALYZE_MODEL), _go)
        else:
            self.notify(
                f"Im Vergleich vorgemerkt, aber {_esc(insurer)} {_esc(product)} ist noch nicht "
                "analysierbar (keine Quell-URLs). \\[H] harvestet + analysiert live; "
                "die Spalte erscheint danach.",
                severity="warning",
                timeout=8,
            )
            self._reload_all()

    def action_manage_compare(self) -> None:
        """Open the Vergleich manager: toggle which analyzed tariffs are in the
            comparison. The single source of truth is compare_stems (an include-set)
            in config/favorites.json — this works on any tab and does not depend on a
            row cursor (the old per-row [c] toggle was ambiguous on the Vergleich
            matrix, whose rows are categories, not tariffs). The pool is every
            analyzed tariff; compare-set members not yet analyzed (queued from the
            Market [a]) are preserved untouched."""
        if not self._details_by_stem:
            self.notify(
                "Noch keine analysierten Tarife — erst \\[g] (Download + Analyse) "
                "oder \\[G] (nur Analyse).",
                severity="information",
                timeout=5,
            )
            return
        ref = self._favorites.get("reference_stem")
        stems = sorted(
            ((stem, _col_label(stem)) for stem in self._details_by_stem),
            key=lambda sl: (sl[0] != ref, sl[0]),
        )
        pool = set(self._details_by_stem)
        included_now = {s for s in self._compare_stems() if s in pool}

        def _apply(new_included: list[str] | None) -> None:
            if new_included is None:
                return  # cancelled — leave compare_stems untouched
            # Keep any queued-but-unanalyzed members (added from the Market): the
            # manager only ever shows the analyzed pool, so it must not drop them.
            kept_pending = [s for s in self._compare_stems() if s not in pool]
            self._set_compare_stems(sorted(set(new_included) | set(kept_pending)))
            self.notify(
                f"Vergleich: {len(new_included)}/{len(stems)} analysierte Tarife.",
                timeout=3,
            )
            self._reload_all()

        self.push_screen(
            CompareManagerScreen(stems, included_now, ref), _apply
        )

    def action_compare_fulltext(self) -> None:
        """Open the per-category full-text modal: every Leistung/Ausschluss
            verbatim across all compared tariffs, untruncated (the cross-tariff
            companion to the [d] detail band, which is per-tariff only)."""
        entries, n_cols = self._fulltext_entries()
        if not entries:
            self.notify(
                "Keine Leistungen/Ausschlüsse zum Anzeigen — erst Tarife "
                "analysieren (\\[g]).",
                severity="information",
                timeout=5,
            )
            return
        self.push_screen(CompareTextScreen(entries, n_cols))

    def action_toggle_compare_wording(self) -> None:
        """Toggle the Vergleich between compact (glyph matrix only) and verbose
            (each insurer's verbatim wording under every shared category)."""
        self._compare_verbose = not self._compare_verbose
        self._populate_coverage()
        self.notify(
            "Vergleich: Wortlaut " + ("an" if self._compare_verbose else "aus"),
            timeout=3,
        )
        self.action_switch_tab("diff")  # make the change visible immediately

    def action_open_source(self) -> None:
        """Open the active tariff's source documents — online (browser) or the
            local PDFs (data/raw/<stem>/) — to read the original."""
        ident = self._active_identity()
        if ident is None:
            self.notify("Erst eine Zeile wählen (Pfeile / Klick).", severity="warning")
            return
        insurer, product, stem = ident
        entry = self._doc_by_stem.get(stem) if stem else None
        docs = (entry or {}).get("docs", [])
        raw_dir = _raw_dir_for_stem(stem) if stem else None
        n_pdfs = len(list(raw_dir.glob("*.pdf"))) if raw_dir and raw_dir.is_dir() else 0
        urls = [d["url"] for d in docs if d.get("url")]
        has_urls = bool(urls)
        if not has_urls and not n_pdfs:
            self.notify(
                "Keine Quell-URLs und keine lokalen PDFs für diese Zeile.",
                severity="warning",
                timeout=6,
            )
            return
        label = f"{insurer} {product}"

        # Skip the modal when only one source type is available.
        if has_urls and not n_pdfs:
            self._open_external(urls)
            return
        if n_pdfs and not has_urls:
            self._open_external([str(raw_dir)])
            return

        def _go(choice: str | None) -> None:
            if choice == "online":
                self._open_external(urls)
            elif choice == "disk" and raw_dir is not None:
                self._open_external([str(raw_dir)])

        self.push_screen(
            OpenSourceScreen(label, docs, len(urls), n_pdfs, stem or ""), _go
        )

    def action_open_offer(self) -> None:
        """Open the active row's CHECK24 offer page in the browser — the explicit
        replacement for the old Enter side effect (Enter now only opens the detail
        band; the same URL stays clickable as the ↗ link in the band)."""
        ident = self._active_identity()
        if ident is None:
            self.notify("Erst eine Zeile wählen (Pfeile / Klick).", severity="warning")
            return
        row = self._active_row
        if row is None or not row.position:
            self.notify(
                "Keine CHECK24-Position für diese Zeile — kein Angebots-Link baubar.",
                severity="warning", timeout=5,
            )
            return
        url = self._build_offer_url(row.position)
        if url:
            self._open_external([url])
        else:
            self.notify(
                "Kein Query-Profil (config/check24-profile.json) — Link nicht baubar.",
                severity="warning", timeout=6,
            )

    def _open_external(self, targets: list[str]) -> None:
        """Hand off URLs / paths to the OS opener (browser / Finder). Best-effort:
            a missing opener or a launch error is surfaced, never fatal."""
        import shutil
        import subprocess

        if not targets:
            self.notify("Nichts zu öffnen.", severity="information")
            return
        opener = "open" if sys.platform == "darwin" else (
            shutil.which("xdg-open") or "xdg-open"
        )
        opened = 0
        for t in targets:
            try:
                subprocess.Popen(
                    [opener, t],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                opened += 1
            except OSError as exc:
                self.notify(f"Öffnen fehlgeschlagen: {exc}", severity="error", timeout=6)
                return
        self.notify(f"{opened} geöffnet.", timeout=3)

    def on_click(self, event) -> None:
        style = getattr(event, "style", None)
        link = style.link if (style is not None and hasattr(style, "link")) else None
        if link and link.startswith("http"):
            self._open_external([link])
            event.stop()

    def _get_offer_url_base(self):
        """Cache (cq_module, base_url, pairs_without_pin) for _build_offer_url.
        Returns None if the profile is missing or malformed. One disk read per
        session; cleared by _load_data() on [r] refresh."""
        if hasattr(self, "_offer_url_cache"):
            return self._offer_url_cache
        from urllib.parse import parse_qsl
        cq = self._load_query_module()
        if cq is None:
            self._offer_url_cache = None
            return None
        loaded = self._load_query_profile()
        if loaded is None:
            self._offer_url_cache = None
            return None
        _, base, query, _ = loaded
        pairs = parse_qsl(query, keep_blank_values=True)
        pairs_no_pin = [(k, v) for k, v in pairs if k not in cq.PIN_KEYS]
        self._offer_url_cache = (cq, base, pairs_no_pin)
        return self._offer_url_cache

    def _build_offer_url(self, position: int) -> str | None:
        """CHECK24 result URL with tariff_position set to `position` and all
        provider/package pins dropped, so the listed tariff is highlighted
        regardless of the saved profile's insurer filter."""
        from urllib.parse import urlencode
        cached = self._get_offer_url_base()
        if cached is None:
            return None
        cq, base, pairs_no_pin = cached
        pairs = cq.set_param(list(pairs_no_pin), "tariff_position", str(position))
        return base + "?" + urlencode(pairs)

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
            # Atomic write (tmp + os.replace), matching _save_favorites and the
            # profile/snapshot/ingest writers: a crash mid-write must not leave a
            # truncated, invalid manifest.json behind.
            tmp = mp.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, mp)

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
            # A stem must be 'insurer__tariff'. An empty part collapses the path to
            # data/<base>/<insurer>/ (rmtree wipes the WHOLE insurer); a '.'/'..' or
            # separator in a part lets rmtree escape data/ entirely — '..__..'
            # resolves to the repo root. Refuse rather than over-delete.
            unsafe = any(
                p in (".", "..") or "/" in p or "\\" in p or "\x00" in p
                for p in (insurer_part, tariff_part)
            )
            if "__" not in stem or not insurer_part or not tariff_part or unsafe:
                self.notify(
                    f"Abbruch: '{stem}' ist kein sicherer insurer__tariff-stem — "
                    f"PDFs/Extrakte nicht gelöscht (Schutz vor Über-Löschung).",
                    severity="error", timeout=8,
                )
            else:
                for base in ("raw", "extracted"):
                    root = (REPO_ROOT / "data" / base).resolve()
                    d = root / insurer_part / tariff_part
                    # Defense in depth: never rmtree at/above data/<base>/.
                    rd = d.resolve()
                    if rd == root or not rd.is_relative_to(root):
                        continue
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

        # Drop the deleted tariff from the Vergleich include-set: its records are
        # gone, so it can never be a column again — left in place it resurfaces
        # forever as a phantom "ausstehend" entry that [c] cannot remove.
        compare = self._compare_stems()
        if stem in compare:
            self._set_compare_stems([s for s in compare if s != stem])
            removed.append("config/favorites.json (compare_stems)")

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
        spec = TAB_SPECS.get(active)
        if spec is None or spec.band_id is None:
            return None
        return spec.band_id, spec.content_id

    def _render_active_into(self, ids: tuple[str, str]) -> None:
        """Render the current active row/favorite into a band's Static.

        Single funnel for all four detail bands: every branch only computes the
        markup, the one update at the end runs it through the containment guard
        — one choke point for the whole detail-band crash class."""
        band_id, content_id = ids
        try:
            content = self.query_one(content_id, Static)
        except NoMatches:
            return
        if band_id == "#fav-detail":
            fav, row = self._active_fav, self._active_row
            if fav is None:
                markup = "[dim]Favoriten-Zeile wählen (Pfeile / Klick).[/dim]"
            elif row is None:
                markup = (
                    f"[bold]{_esc(fav.get('insurer', ''))}[/bold] — "
                    f"[italic]{_esc(fav.get('product', ''))}[/italic]\n\n"
                    "[yellow]Kein passender Tarif im aktuellen Snapshot.[/yellow]\n"
                    "[dim]Liste oder Snapshot ist veraltet — config/favorites.json "
                    "oder scripts/snapshot.py auffrischen.[/dim]"
                )
            else:
                markup = self._render_favorite_detail(row, fav)
        elif band_id == "#verlauf-detail":
            row = self._active_row
            if row is None:
                markup = "[dim]Verlauf-Zeile wählen (Pfeile / Enter).[/dim]"
            else:
                markup = self._render_verlauf_detail(row)
        elif band_id == "#magic-detail":
            row = self._active_row
            score = (
                self._magic_score_by_stem.get(row.stem)
                if (row is not None and row.stem) else None
            )
            if score is None:
                markup = "[dim]Magic-Find-Zeile wählen (Pfeile / Klick).[/dim]"
            else:
                markup = self._render_magic_detail(score, row)
        else:  # market band
            row = self._active_row
            if row is None:
                markup = "[dim]Markt-Zeile wählen (Pfeile / Klick).[/dim]"
            else:
                markup = self._render_market_detail(row)
        content.update(guard_content(markup))

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
        self._detail_visible = band.display  # track user intent for auto-show
        if band.display:
            self._render_active_into(ids)
            band.scroll_home(animate=False)
            band.focus()
        # The table just shrank (open) or grew (close); re-center the cursor once
        # the new layout settles so the selection is never hidden behind the band.
        self.call_after_refresh(self._center_active_cursor)

    def _show_detail(self, focus: bool = False) -> None:
        """Reveal the active tab's detail band and render the active row.
        With focus=True, keyboard focus moves to the band so arrow keys scroll it
        (Esc hands focus back to the table — Tab would switch tabs)."""
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
        band.scroll_home(animate=False)
        if focus:
            band.focus()

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

    def _refresh_verlauf_detail(self) -> None:
        """Re-render the Verlauf band live — only when it is currently shown."""
        try:
            band = self.query_one("#verlauf-detail")
        except NoMatches:
            return
        if band.display:
            self._render_active_into(("#verlauf-detail", "#verlauf-detail-content"))

    # --- On-demand: download + analyze the selected tariff ([g]) ---

    def _pipeline_busy(self) -> bool:
        """True (after a notify) when an analyze pipeline is already running — used
        to refuse a second [g]/[G]/[H]/[F] rather than race the running subprocess."""
        if self._pipeline_running:
            self.notify(
                "Eine Analyse läuft bereits — bitte abwarten.",
                severity="warning",
                timeout=5,
            )
            return True
        return False

    def _claim_pipeline(self) -> bool:
        """Atomically claim the single-flight pipeline slot on the UI thread, at the
        moment a confirm is accepted. The action-dispatch _pipeline_busy() check runs
        BEFORE the confirm modal opens, and App bindings keep firing while a modal is
        up — so a second [g]/[G]/[H]/[F] can stack another confirm and, without this,
        both _go callbacks would launch a worker (the workers set _pipeline_running
        only once they start on their thread, too late to block the sibling). Claiming
        here — single-threaded event loop, so check-then-set is atomic — closes that
        window: the first accepted confirm wins, any later one gets the busy notify.
        Returns False (after notifying) when the slot is already taken."""
        if self._pipeline_running:
            self.notify(
                "Eine Analyse läuft bereits — bitte abwarten.",
                severity="warning",
                timeout=5,
            )
            return False
        self._pipeline_running = True
        return True

    def action_fetch_docs(self) -> None:
        """Resolve the selected row to its harvested source PDFs and, after a
            confirm, download + run the analyze pipeline in the background."""
        if self._pipeline_busy():
            return
        if not self._row_tab_active():
            self.notify("Nur auf Markt/Favoriten/Verlauf — dort eine Zeile wählen.",
                        severity="warning")
            return
        row = self._active_row
        if row is None:
            self.notify("Erst eine Zeile wählen (↵).", severity="warning")
            return
        entry = self._doc_entry(row)
        if not entry or not entry.get("docs"):
            self.notify(
                "Keine geharvesteten Quell-URLs — \\[H] harvestet + analysiert live "
                "(headless Browser).",
                severity="warning",
                timeout=6,
            )
            return
        if self._detail_for_row(row):
            self.notify(
                "Schon analysiert — \\[d] zeigt die Details.", severity="information"
            )
            return

        def _go(confirmed: bool | None) -> None:
            if confirmed and self._claim_pipeline():
                self._run_pipeline(entry, row)

        self.push_screen(ConfirmFetchScreen(entry, ANALYZE_MODEL), _go)

    def action_analyze_local(self) -> None:
        """Analyze a tariff whose source PDFs are ALREADY on disk: ingest →
            extract, no download. For PDFs kept from a prior [g] or dropped in
            manually. Gated on data/raw/<stem>/ containing PDFs; still confirms
            because extract is a paid model call."""
        if self._pipeline_busy():
            return
        if not self._row_tab_active():
            self.notify("Nur auf Markt/Favoriten/Verlauf — dort eine Zeile wählen.",
                        severity="warning")
            return
        row = self._active_row
        if row is None:
            self.notify("Erst eine Zeile wählen (↵).", severity="warning")
            return
        stem = row.stem or resolve_stem(row.insurer, row.product)
        if not stem:
            self.notify("Kein kanonischer stem für diese Zeile.", severity="warning")
            return
        raw_dir = _raw_dir_for_stem(stem)
        if not (raw_dir.is_dir() and any(raw_dir.glob("*.pdf"))):
            self.notify(
                f"Keine lokalen PDFs unter data/raw/{stem}/ — nutze [g] zum Download.",
                severity="warning",
                timeout=6,
            )
            return
        if self._detail_for_row(row):
            self.notify(
                "Schon analysiert — \\[d] zeigt die Details.", severity="information"
            )
            return
        entry = {"stem": stem, "insurer": row.insurer, "tariff": row.product}

        def _go(confirmed: bool | None) -> None:
            if confirmed and self._claim_pipeline():
                self._run_pipeline(entry, row, skip_download=True)

        self.push_screen(
            ConfirmFetchScreen(entry, ANALYZE_MODEL, skip_download=True), _go
        )

    def action_harvest(self) -> None:
        """Harvest the selected tariff's documents from the LIVE CHECK24 page (headless
            Playwright), download them, then ingest → extract — the scripted path for a
            tariff whose source URLs were never harvested (e.g. KS/Auxilia — JURPRIVAT).
            Heavier than [g]/[G] (loads the full all-insurers result page), so it
            confirms first."""
        if self._pipeline_busy():
            return
        if not self._row_tab_active():
            self.notify("Nur auf Markt/Favoriten/Verlauf — dort eine Zeile wählen.",
                        severity="warning")
            return
        row = self._active_row
        if row is None:
            self.notify("Erst eine Zeile wählen (↵).", severity="warning")
            return
        if self._detail_for_row(row):
            self.notify(
                "Schon analysiert — \\[d] zeigt die Details.", severity="information"
            )
            return
        entry = self._doc_entry(row)
        if entry and entry.get("docs"):
            self.notify(
                "URLs schon geharvestet — [g] lädt + analysiert direkt "
                "(kein Headless-Load nötig).",
                severity="information",
                timeout=6,
            )
            return
        # No stem yet — harvest_docs derives it from (insurer, product); the confirm
        # and the start notify use the row label instead.
        pseudo = {"insurer": row.insurer, "tariff": row.product}

        def _go(confirmed: bool | None) -> None:
            if confirmed and self._claim_pipeline():
                self._run_pipeline(pseudo, row, harvest=True)

        self.push_screen(ConfirmFetchScreen(pseudo, ANALYZE_MODEL, harvest=True), _go)

    # --- Magic deep-scan funnel ([F]) ---

    def action_toggle_needs(self) -> None:
        """Toggle the Magic-Find Bedarf view: objective market quality <-> personal
            need-weighted module_breadth (config/needs-weights.json). Only meaningful on
            the Magic tab; re-ranks in place and reselects the top row."""
        try:
            active = self.query_one("#tabs", TabbedContent).active
        except NoMatches:
            return
        if active != "magic":
            self.notify("Bedarf-Modus gilt nur im Magic-Find-Tab \\[M].",
                        severity="warning")
            return
        self._magic_needs_mode = not self._magic_needs_mode
        self._populate_magic()
        self._refresh_magic_detail()
        if self._magic_needs_mode:
            needs = magic.load_needs()
            if magic.needs_are_neutral(needs):
                self.notify(
                    "Bedarf-Modus an — aber Gewichte sind neutral. "
                    "config/needs-weights.json anpassen, damit sich das Ranking ändert.",
                    timeout=7,
                )
            else:
                self.notify("Bedarf-Modus an — Module nach deiner Gewichtung.")
        else:
            self.notify("Bedarf-Modus aus — objektive Marktqualität.")

    def action_edit_needs(self) -> None:
        """Open the Bedarf-weights editor (config/needs-weights.json) in the TUI: set a
            relevance level per Baustein. On save it persists, switches the Magic tab to
            Bedarf mode if the weights are non-neutral, and re-ranks. Works from anywhere
            but the result only shows on the Magic tab."""
        keys_labels = list(MODULE_LABELS.items())
        current = magic.load_needs()

        def _apply(new_weights: dict | None) -> None:
            if new_weights is None:
                return  # cancelled
            try:
                magic.save_needs(new_weights)
            except OSError as exc:
                self.notify(f"Speichern fehlgeschlagen: {exc}",
                            severity="error", timeout=8)
                return
            neutral = magic.needs_are_neutral(magic.load_needs())
            # A non-neutral save is only visible in Bedarf mode — switch it on so the
            # user sees the effect immediately; a neutral save leaves the mode as is.
            if not neutral:
                self._magic_needs_mode = True
            self._populate_magic()
            self._refresh_magic_detail()
            self.notify(
                "Bedarf-Gewichte gespeichert — neutral, Ranking unverändert."
                if neutral else
                "Bedarf-Gewichte gespeichert — Bedarf-Modus aktiv, Ranking neu.",
                timeout=5,
            )

        self.push_screen(NeedsEditorScreen(keys_labels, current), _apply)

    def action_magic_scan(self) -> None:
        """Deep-scan funnel: prescore the whole market, then harvest + analyze the
            top-pool_k candidates that still lack an analyzed record, and re-rank.
            Market-wide — needs no row selection. Shares the [g]/[G]/[H] single-flight
            guard so it can't race a running pipeline. Magic stays read-only over
            out/tariffs (price is shown, never scored)."""
        if self._pipeline_busy():
            return
        if not self._snapshot:
            self.notify("Kein Snapshot geladen — nichts zu scannen.",
                        severity="warning")
            return
        weights = magic.load_weights()
        pre = magic.prescore(self._snapshot.rows)
        selected, dropped = magic.select_candidates(pre, weights.pool_k)
        missing = [p for p in selected if not p.has_detail]
        if not missing:
            self.notify(
                f"Top {len(selected)} sind schon analysiert — Ranking ist aktuell.",
                timeout=6,
            )
            return
        candidates = [(p.insurer, p.product) for p in missing]

        def _go(confirmed: bool | None) -> None:
            if confirmed and self._claim_pipeline():
                self._run_magic_scan(candidates, len(dropped), len(selected))

        self.push_screen(
            MagicScanScreen(candidates, len(dropped), len(selected), ANALYZE_MODEL),
            _go,
        )

    def action_update_all(self) -> None:
        """Full market refresh ([U]): live scan+snapshot, manifest PDF downloads,
            then ingest → extract → overlay → render → regression — the TUI twin
            of ./update-all.sh. Market-wide, needs no row selection; shares the
            single-flight guard. Extract flags come from the dominant record
            provenance (cache-signature match: unchanged tariffs cost nothing);
            an explicitly set CHECK0R_ANALYZE_MODEL still wins for the model."""
        if self._pipeline_busy():
            return
        prov_model, filter_on, repeat = dominant_provenance()
        model = (ANALYZE_MODEL if "CHECK0R_ANALYZE_MODEL" in os.environ
                 else (prov_model or ANALYZE_MODEL))
        n_records = len(list((REPO_ROOT / "out" / "tariffs").glob("*.json")))

        def _go(confirmed: bool | None) -> None:
            if confirmed and self._claim_pipeline():
                self._run_update_all(model, filter_on, repeat)

        self.push_screen(UpdateAllScreen(model, filter_on, repeat, n_records), _go)

    # --- Live pipeline status line (bottom, above the Footer) ---

    def _set_pipeline_status(self, markup: str) -> None:
        """Replace the persistent status line with live pipeline progress. Runs on
            the UI thread (callers use call_from_thread). Unlike a toast this stays
            put, so a failed stage + its log tail remain readable until the next
            reload — which is the whole point: see what broke without scrollback.

            While a pipeline is active the animated tui_anim loader bar is
            prepended; the raw markup is kept so _animate_pipeline_status can
            re-render it every tick (and once more without the bar at the end)."""
        self._pipeline_status_markup = markup
        if self._pipeline_running:
            markup = f"{tui_anim.loader_markup(self._loader_tick)} {markup}"
        try:
            self.query_one("#status-bar", Label).update(guard_content(markup))
        except NoMatches:
            pass
        self._update_loader_overlay()

    def _update_loader_overlay(self) -> None:
        """Show/refresh the fat centered loader while a pipeline runs (big
            animated fire bar + the current stage line), hide it when idle."""
        try:
            layer = self.query_one("#loader-layer", Container)
            box = self.query_one("#loader-overlay", Static)
        except NoMatches:
            return
        if self._pipeline_running:
            layer.display = True
            box.update(guard_content(
                f"{tui_anim.loader_big_markup(self._loader_tick)}\n\n"
                f"{self._pipeline_status_markup or '[dim]startet …[/dim]'}"
            ))
        else:
            layer.display = False

    def _animate_pipeline_status(self) -> None:
        """Interval-driven (~11 fps): advance the loader bar/overlay while a
            pipeline runs; when the run ends, redraw the final ✓/✗ status once
            without the bar and hide the overlay."""
        if self._pipeline_running:
            self._loader_tick += 1
            self._loader_was_active = True
            self._set_pipeline_status(self._pipeline_status_markup)
        elif self._loader_was_active:
            self._loader_was_active = False
            self._set_pipeline_status(self._pipeline_status_markup)

    def _stream_step(
        self, name: str, cmd: list[str], timeout: int, log_path: Path,
        *, idx: int, total: int,
    ) -> tuple[bool, str]:
        """Run one pipeline subprocess via tui_data.stream_subprocess, mirroring its
            progress into the persistent status line and the full output into
            log_path. Returns (ok, reason).

            On failure (non-zero exit, timeout, or spawn error) the status line keeps
            the failing stage + its last output line visible — a toast would vanish —
            and points at the log file for the full trace."""
        prefix = f"[{idx}/{total}] {_esc(name)}"
        log_rel = f"tmp/{log_path.name}"
        self.call_from_thread(
            self._set_pipeline_status,
            f"[yellow]⏳ {prefix}[/yellow] [dim]startet …[/dim]",
        )
        res = stream_subprocess(
            cmd, REPO_ROOT, timeout, log_path,
            on_line=lambda s: self.call_from_thread(
                self._set_pipeline_status,
                f"[yellow]⏳ {prefix}[/yellow] [dim]{_esc(s[:110])}[/dim]",
            ),
        )
        if res.ok:
            self.call_from_thread(
                self._set_pipeline_status,
                f"[green]✓ {prefix}[/green] [dim]fertig[/dim]",
            )
            return True, ""
        if res.spawn_error is not None:
            self.call_from_thread(
                self._set_pipeline_status,
                f"[red]✗ {prefix}[/red] [dim]{_esc(res.spawn_error)}[/dim]",
            )
            return False, res.spawn_error
        if res.timed_out:
            reason = f"Timeout nach {timeout}s"
            self.call_from_thread(
                self._set_pipeline_status,
                f"[red]✗ {prefix} — {reason}[/red] [dim]Log: {log_rel}[/dim]",
            )
            return False, reason
        reason = res.reason or f"exit {res.returncode}"
        self.call_from_thread(
            self._set_pipeline_status,
            f"[red]✗ {prefix} fehlgeschlagen (exit {res.returncode})[/red] "
            f"[dim]{_esc(reason[:90])} · Log: {log_rel}[/dim]",
        )
        return False, reason

    def _run_pipeline_tail(self, log_path: Path, *, base_idx: int, total: int,
                           local_model: bool) -> None:
        """Run the post-extract tail (Overlay → Render → Regression) that pipeline.sh
            runs after extract, so a TUI-started analysis reaches the same on-disk state
            (out/enriched, out/vergleich.md, the golden gate) instead of stopping at
            Extract. Shared by the [g]/[G]/[H] pipeline and the [F] magic-scan funnel.

            Warn-only, exactly like pipeline.sh: a failing step surfaces a warning
            (status line + toast) but never aborts the chain or fails the run — the
            extract results are already on disk. Render mirrors how Extract is launched
            (same --model + local-cold timeout); overlay/regression are model-free and
            fast, so they get a modest ceiling."""
        tail = [
            ("Overlay", ["uv", "run", "scripts/overlay.py"], 300),
            ("Render",
             ["uv", "run", "scripts/render.py", "--model", ANALYZE_MODEL],
             1200 if local_model else 600),
            ("Regression", ["uv", "run", "scripts/regression.py"], 300),
        ]
        for offset, (name, cmd, step_timeout) in enumerate(tail):
            ok, reason = self._stream_step(
                name, cmd, step_timeout, log_path,
                idx=base_idx + offset, total=total,
            )
            if not ok:
                self.call_from_thread(
                    self.notify,
                    f"{name} fehlgeschlagen (nicht fatal): {_esc(reason[:90])}",
                    severity="warning",
                    timeout=7,
                )

    @work(thread=True, group="pipeline")
    def _run_magic_scan(self, candidates: list[tuple[str, str]], n_dropped: int,
                        n_selected: int) -> None:
        """Run the market-scan funnel off the UI thread. Shares _pipeline_running with
            _run_pipeline (set here, cleared in finally) so the action-layer guard
            refuses a second start rather than racing this one's subprocesses.

            @work is load-bearing: without it this runs ON the UI thread and the
            first call_from_thread in _stream_step raises RuntimeError (verified) —
            [F]-confirm would crash before the harvest even starts."""
        self._pipeline_running = True
        try:
            self._run_magic_scan_steps(candidates, n_dropped, n_selected)
        finally:
            self._pipeline_running = False

    def _run_magic_scan_steps(self, candidates: list[tuple[str, str]], n_dropped: int,
                              n_selected: int) -> None:
        # Write the candidate list where harvest_docs --select-file reads it: repo
        # tmp/ (gitignored), so ONE Playwright session harvests all of them. Guarded
        # like the subprocess steps below — a write failure (tmp/ is a file, read-only
        # FS, disk full) must surface a notify, not escape the worker thread silently.
        sel_dir = REPO_ROOT / "tmp"
        sel_path = sel_dir / "magic-scan-select.json"
        try:
            sel_dir.mkdir(exist_ok=True)
            sel_path.write_text(
                json.dumps([{"insurer": i, "product": p} for i, p in candidates],
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            self.call_from_thread(
                self.notify, f"Scan-Vorbereitung fehlgeschlagen: {exc}",
                severity="error", timeout=8,
            )
            return
        self.call_from_thread(
            self.notify,
            f"Markt-Scan: {len(candidates)} Kandidat(en) harvesten + analysieren …",
            timeout=5,
        )
        if n_dropped:
            self.call_from_thread(
                self.notify,
                f"{n_dropped} weitere mit gleichem Vorab-Score außerhalb Top-"
                f"{n_selected} — pool_k erhöhen für mehr.",
                severity="information",
                timeout=7,
            )
        local_model = _providers.parse_spec(ANALYZE_MODEL)[0] != "claude"
        # The harvest pulls K panels in one headless pass; extract then analyzes all K
        # pending records. Both scale with K, so give them generous ceilings (a local
        # cold model is slowest) — this is a long, user-initiated batch.
        steps = [
            ("Harvest+Download",
             ["uv", "run", "scripts/harvest_docs.py",
              "--select-file", str(sel_path), "--download", "--jobs", "6"], 1800),
            ("Ingest", ["uv", "run", "scripts/ingest.py"], 600),
            ("Extract",
             ["uv", "run", "scripts/extract.py", "--model", ANALYZE_MODEL],
             3600 if local_model else 2400),
        ]
        # One combined log per scan run; truncate so a previous run's output can't be
        # mistaken for this one's. _stream_step appends each step under a header.
        log_path = sel_dir / "magic-scan.log"
        try:
            log_path.write_text("", encoding="utf-8")
        except OSError:
            pass
        total = len(steps) + 3  # + Overlay/Render/Regression tail
        for idx, (name, cmd, step_timeout) in enumerate(steps, 1):
            ok, _reason = self._stream_step(
                name, cmd, step_timeout, log_path, idx=idx, total=total,
            )
            if not ok:
                return
        self._run_pipeline_tail(log_path, base_idx=len(steps) + 1, total=total,
                                local_model=local_model)
        self.call_from_thread(self._after_magic_scan, len(candidates))

    def _after_magic_scan(self, n: int) -> None:
        """Reload from disk so the freshly analyzed candidates enter the ranking, then
            jump to the Magic tab so the user sees the updated top pick."""
        self._reload_all()
        try:
            self.query_one("#tabs", TabbedContent).active = "magic"
        except NoMatches:
            pass
        self.notify(
            f"Markt-Scan fertig: {n} Tarif(e) analysiert — Ranking aktualisiert.",
            timeout=8,
        )

    @work(thread=True, group="pipeline")
    def _run_update_all(self, model: str, filter_on: bool, repeat: int) -> None:
        """Run the full market refresh off the UI thread. Shares _pipeline_running
            with the other funnels (set here, cleared in finally)."""
        self._pipeline_running = True
        try:
            self._run_update_all_steps(model, filter_on, repeat)
        finally:
            self._pipeline_running = False

    def _run_update_all_steps(self, model: str, filter_on: bool, repeat: int) -> None:
        # Mirrors ./update-all.sh: scan and docs failures warn but don't abort
        # (the pipeline still runs on local data); ingest/extract failures abort
        # like the other funnels. Extract carries the provenance flags so the
        # cache signature matches the existing records.
        local_model = _providers.parse_spec(model)[0] != "claude"
        extract_cmd = ["uv", "run", "scripts/extract.py", "--model", model]
        if filter_on:
            extract_cmd.append("--filter")
        if repeat > 1:
            extract_cmd += ["--repeat", str(repeat)]
        steps = [
            ("Scan+Snapshot",
             ["uv", "run", "scripts/fetch_ratings.py", "--snapshot"], 600, False),
            ("Docs",
             ["uv", "run", "scripts/fetch_docs.py", "--apply", "--into-raw"],
             1800, False),
            ("Ingest", ["uv", "run", "scripts/ingest.py"], 600, True),
            # Ceiling, not an estimate: cache-current tariffs are skipped, so the
            # common run is fast — but a doc-refresh wave re-extracts many stems
            # at repeat×cost, and a cold local model is slowest.
            ("Extract", extract_cmd, 5400 if local_model else 3600, True),
        ]
        log_path = REPO_ROOT / "tmp" / "update-all.log"
        try:
            log_path.parent.mkdir(exist_ok=True)
            log_path.write_text("", encoding="utf-8")
        except OSError:
            pass
        total = len(steps) + 3  # + Overlay/Render/Regression tail
        for idx, (name, cmd, step_timeout, fatal) in enumerate(steps, 1):
            ok, reason = self._stream_step(
                name, cmd, step_timeout, log_path, idx=idx, total=total,
            )
            if not ok:
                if fatal:
                    return
                self.call_from_thread(
                    self.notify,
                    f"{name} fehlgeschlagen (nicht fatal): {_esc(reason[:90])}",
                    severity="warning",
                    timeout=7,
                )
        self._run_pipeline_tail(log_path, base_idx=len(steps) + 1, total=total,
                                local_model=local_model)
        self.call_from_thread(self._after_update_all)

    def _after_update_all(self) -> None:
        """Reload from disk and jump to Verlauf — the fresh snapshot's diff plus
            the market-over-time line are the natural "what changed" view."""
        self._reload_all()
        try:
            self.query_one("#tabs", TabbedContent).active = "verlauf"
        except NoMatches:
            pass
        self.notify(
            "Update-All fertig — Verlauf zeigt den neuen Snapshot.", timeout=8,
        )

    @work(thread=True, group="prewarm")
    def _prewarm_analyze_model(self) -> None:
        """Load a local analyze model into the server's RAM at startup, off the UI
        thread, so the first [g]/[G]/[H] analysis runs warm instead of paying the
        one-time cold-load (minutes for a large local model) inline in the Extract
        step — which would otherwise overrun its subprocess timeout. No-op for the
        claude backend (no resident model to warm). Failure is a soft warning: the
        extraction still works, it just starts cold."""
        provider, _, _ = _providers.parse_spec(ANALYZE_MODEL)
        if provider == "claude":
            return
        self.call_from_thread(
            self.notify, f"Lokales Modell wird vorgeladen: {_esc(ANALYZE_MODEL)} …",
            timeout=4,
        )
        res = _providers.prewarm(ANALYZE_MODEL)
        if res["ok"]:
            self.call_from_thread(
                self.notify,
                f"Modell bereit ({res['wall_s']:.0f}s): {_esc(ANALYZE_MODEL)}",
                timeout=4,
            )
        else:
            self.call_from_thread(
                self.notify,
                f"Vorladen fehlgeschlagen (Analyse startet kalt): "
                f"{_esc(res['error'] or '?')}",
                severity="warning",
                timeout=8,
            )

    @work(thread=True, group="pipeline")
    def _run_pipeline(self, entry: dict, row: SnapshotRow,
                      *, skip_download: bool = False, harvest: bool = False) -> None:
        """Run the analyze pipeline for one tariff off the UI thread; status is
            posted back via call_from_thread. With harvest, a headless harvest_docs
            pass (--download) fetches the source URLs + PDFs first; with skip_download
            the PDFs are already in data/raw/<stem>/ and only ingest → extract run;
            otherwise fetch_docs --into-raw downloads them first.

            Guarded by _pipeline_running (set here, cleared in finally) so a second
            invocation is refused at the action layer rather than racing the OS
            subprocess of this one."""
        self._pipeline_running = True
        try:
            self._run_pipeline_steps(entry, row, skip_download=skip_download,
                                     harvest=harvest)
        finally:
            self._pipeline_running = False

    def _run_pipeline_steps(self, entry: dict, row: SnapshotRow, *,
                            skip_download: bool, harvest: bool) -> None:
        stem = entry.get("stem", "")
        label = stem or f"{row.insurer} {row.product}"
        # Download straight into the canonical data/raw/<stem>/ layout (--into-raw),
        # so ingest/extract name the record exactly <stem>.json — no filename-guessing
        # intake step that could misname it and hide the result from the TUI.
        steps = []
        if harvest:
            # Pin EXACTLY this tariff on the fresh page (--exact + --insurer), harvest
            # its document URLs into the manifest, and download into data/raw/ in one
            # headless pass; then ingest/extract pick the PDFs up by stem.
            steps.append((
                "Harvest",
                ["uv", "run", "scripts/harvest_docs.py",
                 "--insurer", row.insurer, "--match", row.product, "--exact",
                 "--download", "--jobs", "6"],
            ))
        elif not skip_download:
            steps.append(
                ("Download", ["uv", "run", "scripts/fetch_docs.py", stem, "--into-raw"])
            )
        steps += [
            ("Ingest", ["uv", "run", "scripts/ingest.py"]),
            ("Extract", ["uv", "run", "scripts/extract.py", "--model", ANALYZE_MODEL]),
        ]
        # A local model that is not yet resident pays a one-time cold-load (minutes)
        # on top of inference, which can exceed the default 600s step budget. Give the
        # Extract step the same 1200s ceiling _providers.run() already allows for the
        # model call, so a cold-start race (analyze fired before the mount prewarm
        # finished) doesn't get killed mid-load.
        local_model = _providers.parse_spec(ANALYZE_MODEL)[0] != "claude"
        self.call_from_thread(
            self.notify, f"Pipeline gestartet: {_esc(label)} …", timeout=4
        )
        log_path = REPO_ROOT / "tmp" / "pipeline.log"
        try:
            log_path.parent.mkdir(exist_ok=True)
            log_path.write_text("", encoding="utf-8")
        except OSError:
            pass
        total = len(steps) + 3  # + Overlay/Render/Regression tail
        for idx, (name, cmd) in enumerate(steps, 1):
            step_timeout = 1200 if (name == "Extract" and local_model) else 600
            ok, _reason = self._stream_step(
                name, cmd, step_timeout, log_path, idx=idx, total=total,
            )
            if not ok:
                return
        self._run_pipeline_tail(log_path, base_idx=len(steps) + 1, total=total,
                                local_model=local_model)
        self.call_from_thread(self._after_pipeline, row)

    def _after_pipeline(self, row: SnapshotRow) -> None:
        """Reload data so the freshly extracted record shows, then refresh panels.
        Delegates to _reload_all so the Verlauf tab and the held selection are
        restored too (not just the market/favorites tables)."""
        self._reload_all()
        self.notify(
            f"Analyse fertig: {_esc(row.insurer)} {_esc(row.product)} — \\[d] zeigt die Details.",
            timeout=8,
        )
