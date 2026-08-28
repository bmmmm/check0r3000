#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "textual>=8.0",
# ]
# ///
"""Live Textual runtime test — drives the real CheckApp through Textual's
`run_test()` Pilot and asserts the interaction invariants that the textual-free
`tui_data.py --selftest` cannot reach: tab switching, the cross-tab active-state
reconciliation (the [u]/[R]/[D] desync class), the detail-band toggle, the Market
filter debounce, the help modal, and that navigating over markup-hostile data does
not raise MarkupError on the live render path.

Runs against the real on-disk snapshot/favorites (no network, no API). Each case
gets a fresh app instance for isolation. Exit 0 = all passed, non-zero otherwise.

    uv run scripts/tui_test.py
    .venv/bin/python scripts/tui_test.py    # offline / sandboxed
"""
from __future__ import annotations

import asyncio
import dataclasses
import sys
import traceback
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from textual.widgets import DataTable, Label, Static, TabbedContent  # noqa: E402

from tui_app import CheckApp  # noqa: E402

TEST_SIZE = (160, 50)


# --- helpers ---------------------------------------------------------------

def _active_tab(app: CheckApp) -> str | None:
    try:
        return app.query_one("#tabs", TabbedContent).active
    except Exception:
        return None


def _table(app: CheckApp, table_id: str) -> DataTable:
    return app.query_one(table_id, DataTable)


def _cursor_key(table: DataTable) -> str | None:
    try:
        rk = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
    except Exception:
        return None
    return None if rk is None else str(rk)


async def _wait_until(pilot, pred, timeout: float = 2.0, step: float = 0.05) -> bool:
    """Poll `pred` until true or timeout — for debounced/after-refresh effects
    (the Market filter's 0.15s timer, call_after_refresh centering)."""
    waited = 0.0
    while waited < timeout:
        if pred():
            return True
        await pilot.pause()
        await asyncio.sleep(step)
        waited += step
    return bool(pred())


# --- test cases ------------------------------------------------------------
# Each takes (app, pilot) and raises AssertionError on failure.

async def t_boot_and_tables(app, pilot) -> None:
    """App mounts; the initial tab is Favorites; both tables are populated and
    their row counts match the in-memory row maps (no silent row loss)."""
    assert _active_tab(app) == "favorites", f"initial tab {_active_tab(app)!r}"
    market = _table(app, "#market-table")
    fav = _table(app, "#fav-table")
    assert market.row_count > 0, "market table empty — no snapshot loaded?"
    assert fav.row_count > 0, "favorites table empty"
    assert market.row_count == len(app._market_rows), (
        f"market rows {market.row_count} != map {len(app._market_rows)}")
    assert fav.row_count == len(app._fav_rows), (
        f"fav rows {fav.row_count} != map {len(app._fav_rows)}")


async def t_tab_shortcuts(app, pilot) -> None:
    """The global single-letter bindings switch tabs."""
    for key, want in (("x", "market"), ("v", "diff"), ("l", "verlauf"),
                      ("B", "bench"), ("y", "favorites")):
        await pilot.press(key)
        await pilot.pause()
        assert _active_tab(app) == want, f"[{key}] -> {_active_tab(app)!r}, want {want!r}"


async def t_tab_cycle(app, pilot) -> None:
    """Tab cycles forward through _TAB_ORDER and wraps; Shift+Tab goes back."""
    assert _active_tab(app) == "favorites"
    for want in ("market", "diff", "verlauf", "bench", "magic", "favorites"):
        await pilot.press("tab")
        await pilot.pause()
        assert _active_tab(app) == want, f"tab -> {_active_tab(app)!r}, want {want!r}"
    await pilot.press("shift+tab")
    await pilot.pause()
    assert _active_tab(app) == "magic", f"shift+tab -> {_active_tab(app)!r}"


def _shown_footer_keys(app) -> set[str]:
    """The keys the Footer would show right now: active_bindings filtered on the
    binding's show flag. active_bindings walks the focused widget's ancestor chain
    (which includes the active TabPane) up through the screen and app — so a pane's
    contextual BINDINGS appear only while its tab is active."""
    return {k for k, ab in app.screen.active_bindings.items() if ab.binding.show}


async def t_contextual_footer(app, pilot) -> None:
    """The footer is per-tab: each pane's contextual keys surface only while its
    tab is active (per-pane BINDINGS on the TabPane ancestor), while the tab-switch
    keys leave the footer yet keep dispatching. Market shows the sort/filter keys
    but not Verlauf's [m] or Magic's [P]; Verlauf shows [m] but not the market sort
    [s]; Magic shows [P] but not [m]. The switch key [x] is hidden but still works."""
    await pilot.press("x")
    await pilot.pause()
    market = _shown_footer_keys(app)
    assert {"s", "f"} <= market, f"market footer missing sort/filter keys: {sorted(market)}"
    assert "m" not in market and "P" not in market, (
        f"market footer leaks another tab's keys: {sorted(market)}")

    await pilot.press("l")
    await pilot.pause()
    verlauf = _shown_footer_keys(app)
    assert "m" in verlauf, f"verlauf footer missing snapshot-filter [m]: {sorted(verlauf)}"
    assert "s" not in verlauf, f"verlauf footer leaks the market sort [s]: {sorted(verlauf)}"

    await pilot.press("M")
    await pilot.pause()
    magic = _shown_footer_keys(app)
    assert "P" in magic, f"magic footer missing Bedarf [P]: {sorted(magic)}"
    assert "m" not in magic, f"magic footer leaks the verlauf filter [m]: {sorted(magic)}"

    # Tab-switch key [x] is no longer shown anywhere, but still switches tabs.
    assert "x" not in magic, f"tab-switch [x] should be hidden from the footer: {sorted(magic)}"
    await pilot.press("x")
    await pilot.pause()
    assert _active_tab(app) == "market", f"[x] no longer switches tabs: {_active_tab(app)!r}"


async def t_cross_tab_roundtrip(app, pilot) -> None:
    """A selection held in Market survives a round-trip through a tab that owns no
    table (Vergleich), restoring the exact held row. (Round-tripping through another
    *table* tab intentionally re-points _held_ident at that tab's cursor — so we use
    the table-less Vergleich tab to isolate the Market restore logic.) The held row is
    chosen with a unique bare key so the bare->first-occurrence restore is unambiguous."""
    await pilot.press("x")
    await pilot.pause()
    market = _table(app, "#market-table")
    # Pick a row whose bare key (insurer|product|SB) occurs exactly once, so
    # _market_ident_to_rk's first-occurrence restore maps back to this very row.
    counts = Counter(r.key for r in app._market_rows.values() if r.key)
    uniq_rk = next((k for k, r in app._market_rows.items()
                    if r.key and counts[r.key] == 1), None)
    assert uniq_rk is not None, "no market row with a unique bare key"
    market.move_cursor(row=market.get_row_index(uniq_rk))
    await pilot.pause()
    rk = _cursor_key(market)
    assert rk == uniq_rk, f"cursor {rk!r} != target {uniq_rk!r}"
    held_row = app._market_rows[uniq_rk]
    assert app._active_row is held_row, "active_row not the highlighted market row"
    assert app._held_ident == held_row.key, "_held_ident not the held row's key"

    await pilot.press("v")   # leave to Vergleich (no table -> held is preserved)
    await pilot.pause()
    await pilot.press("x")   # ...and back
    assert await _wait_until(pilot, lambda: _cursor_key(market) == uniq_rk), (
        f"cursor not restored to held row: {_cursor_key(market)!r} != {uniq_rk!r}")
    assert app._active_row is not None and app._active_row.key == held_row.key, (
        "active_row not re-reconciled to the held market row after round-trip")
    assert app._held_ident == held_row.key, "_held_ident drifted after round-trip"


async def t_cross_tab_held_absent(app, pilot) -> None:
    """The desync guard: a tariff held in Market that is NOT in Favorites must not
    leave the active-state pointing at the Market row once Favorites is shown —
    _adopt_cursor_row reconciles to whatever the Favorites cursor is actually on."""
    await pilot.press("x")
    await pilot.pause()
    market = _table(app, "#market-table")
    # A market row whose bare tariff key is absent from the favorites board
    # (almost all of them are). _held_ident is the bare key, so compare on r.key.
    absent_rk = next((k for k, r in app._market_rows.items()
                      if r.key and r.key not in app._fav_ident_to_rk), None)
    assert absent_rk is not None, "no market row outside favorites — cannot test"
    market.move_cursor(row=market.get_row_index(absent_rk))
    await pilot.pause()
    market_only = app._held_ident
    assert market_only == app._market_rows[absent_rk].key, (
        f"held {market_only!r} != moved-to row key {app._market_rows[absent_rk].key!r}")
    assert market_only not in app._fav_ident_to_rk

    await pilot.press("y")
    await pilot.pause()
    assert _active_tab(app) == "favorites"
    assert app._active_fav is not None, (
        "active_fav is None on Favorites — state stranded on the Market row")
    assert app._held_ident != market_only, (
        "_held_ident still the Market-only key — reconciliation did not run")


async def t_detail_toggle(app, pilot) -> None:
    """[d] flips the active tab's detail band and keeps _detail_visible in sync."""
    await pilot.press("x")
    await pilot.pause()
    band = app.query_one("#detail-panel")
    before = band.display
    await pilot.press("d")
    await pilot.pause()
    assert band.display == (not before), "band did not toggle"
    assert app._detail_visible == band.display, "_detail_visible out of sync"
    await pilot.press("d")
    await pilot.pause()
    assert band.display == before, "band did not toggle back"
    assert app._detail_visible == band.display, "_detail_visible out of sync (2)"


async def t_filter_debounce(app, pilot) -> None:
    """[f] focuses the filter; typing narrows the Market table after the 0.15s
    debounce; Escape clears it back to the full row set."""
    await pilot.press("x")
    await pilot.pause()
    market = _table(app, "#market-table")
    full = market.row_count
    # An insurer guaranteed present (drawn from the live snapshot, lowercased token).
    needle = app._snapshot.rows[0].insurer.split()[0].lower()
    await pilot.press("f")
    await pilot.pause()
    await pilot.press(*needle)
    assert await _wait_until(pilot, lambda: market.row_count < full), (
        f"filter {needle!r} did not narrow rows after debounce (still "
        f"{market.row_count}/{full})")
    assert market.row_count > 0, f"filter {needle!r} matched nothing"
    await pilot.press("escape")
    assert await _wait_until(pilot, lambda: market.row_count == full), (
        f"escape did not restore full rows ({market.row_count}/{full})")


async def t_enter_detail_no_browser(app, pilot) -> None:
    """Enter on a Market row opens + focuses the detail band and does NOT launch
    the browser (the old surprising side effect); Esc hands focus back to the
    table. Pins the [O]-decoupling behavior."""
    opened: list[list[str]] = []
    app._open_external = lambda targets: opened.append(list(targets))  # type: ignore[method-assign]
    await pilot.press("x")
    await pilot.pause()
    market = _table(app, "#market-table")
    market.move_cursor(row=0)
    await pilot.pause()
    market.focus()
    await pilot.press("enter")
    await pilot.pause()
    band = app.query_one("#detail-panel")
    assert band.display, "enter did not open the detail band"
    assert not opened, f"enter launched the browser: {opened}"
    assert app.focused is band, f"band not focused after enter (focused={app.focused})"
    await pilot.press("escape")
    await pilot.pause()
    assert app.focused is market, (
        f"escape did not return focus to the table (focused={app.focused})")
    # [O] is the explicit opener — it must hit _open_external with the offer URL.
    await pilot.press("O")
    await pilot.pause()
    if app._active_row is not None and app._active_row.position and app._build_offer_url(
            app._active_row.position):
        assert opened and opened[0], "[O] did not open the offer URL"


async def t_market_sort_toggle(app, pilot) -> None:
    """[s] sorts the Market table by price ascending and marks the column header
    with ▲; a second [s] flips to descending (▼). A sort key pressed on another
    tab must leave the market sort untouched (scope guard)."""
    await pilot.press("x")
    await pilot.pause()
    market = _table(app, "#market-table")

    def price_header() -> str:
        return list(market.columns.values())[6].label.plain

    await pilot.press("s")
    await pilot.pause()
    assert app.sort_col == "price" and app.sort_asc, "s did not sort price asc"
    assert "▲" in price_header(), f"no asc indicator in header {price_header()!r}"
    prices = [r.monatlich_eur for r in app._visible_rows() if r.monatlich_eur is not None]
    assert prices == sorted(prices), "rows not price-ascending after [s]"

    await pilot.press("s")
    await pilot.pause()
    assert app.sort_col == "price" and not app.sort_asc, "second s did not flip direction"
    assert "▼" in price_header(), f"no desc indicator in header {price_header()!r}"

    await pilot.press("y")  # Favorites — sort keys are market-scoped
    await pilot.pause()
    await pilot.press("n")
    await pilot.pause()
    assert app.sort_col == "price", "sort key leaked from the Favorites tab"


async def t_market_sort_bew(app, pilot) -> None:
    """Clicking the Bew. header (column index 5) sorts by customer rating,
    descending on the first click (best rating first) with None-rating rows
    pushed to the end in both directions; a second click flips to ascending."""
    await pilot.press("x")
    await pilot.pause()
    market = _table(app, "#market-table")

    def bew_header() -> str:
        return list(market.columns.values())[5].label.plain

    app.on_header_selected(SimpleNamespace(column_index=5))
    await pilot.pause()
    assert app.sort_col == "bew" and not app.sort_asc, "header click did not sort bew desc"
    assert "▼" in bew_header(), f"no desc indicator in header {bew_header()!r}"

    bews = [r.bewertung for r in app._visible_rows()]
    rated = [b for b in bews if b is not None]
    assert rated == sorted(rated, reverse=True), "rows not bewertung-descending"
    none_count = sum(1 for b in bews if b is None)
    if none_count:
        assert all(b is None for b in bews[-none_count:]), (
            "None-bewertung rows not at the end (desc)")

    app.on_header_selected(SimpleNamespace(column_index=5))
    await pilot.pause()
    assert app.sort_col == "bew" and app.sort_asc, "second click did not flip to asc"
    assert "▲" in bew_header(), f"no asc indicator in header {bew_header()!r}"

    bews = [r.bewertung for r in app._visible_rows()]
    rated = [b for b in bews if b is not None]
    assert rated == sorted(rated), "rows not bewertung-ascending"
    none_count = sum(1 for b in bews if b is None)
    if none_count:
        assert all(b is None for b in bews[-none_count:]), (
            "None-bewertung rows not at the end (asc)")


async def t_help_modal(app, pilot) -> None:
    """[?] pushes the help modal; Escape pops it."""
    base = len(app.screen_stack)
    await pilot.press("question_mark")
    assert await _wait_until(pilot, lambda: len(app.screen_stack) > base), (
        "help modal did not open")
    await pilot.press("escape")
    assert await _wait_until(pilot, lambda: len(app.screen_stack) == base), (
        "help modal did not close on escape")


async def t_markup_hostile_nav(app, pilot) -> None:
    """Navigating over a row whose text carries Rich-markup metacharacters must not
    raise MarkupError on the live render path (the [/x]-in-data crash class)."""
    await pilot.press("x")
    await pilot.pause()
    hostile = "[/x][bold]BOOM[blink]"
    base = app._snapshot.rows[0]
    app._snapshot.rows.append(dataclasses.replace(
        base, position=999, insurer=hostile, product=hostile,
        selbstbeteiligung=hostile, key="hostile|row|test"))
    app._populate_market_table()           # re-render with the hostile row
    await pilot.pause()
    market = _table(app, "#market-table")
    # The table key is the bare key + "#i" suffix; find it and highlight it.
    hk = next((k for k in app._market_rows if k.startswith("hostile|row|test")), None)
    assert hk is not None, "hostile row not populated"
    market.move_cursor(row=market.get_row_index(hk))   # highlight it -> renders detail
    await pilot.pause()
    app._detail_visible = True
    app._show_detail()                     # force the detail render too
    await pilot.pause()
    # Reaching here without a MarkupError is the real assertion.


async def t_hostile_data_sweep(app, pilot) -> None:
    """The dedicated hostile-data suite behind the _esc choke-point rework
    (issue #1): drive Rich-markup metacharacters through EVERY externally
    sourced data surface (scraped snapshot fields, LLM record fields, manifest
    docs, favorites/tags/notes, external ratings, feature changelogs) and

    1. require that no producer relies on the containment guard — the
       MARKUP_FALLBACKS ledger must stay empty (per-site _esc is complete), and
    2. prove the guard itself catches deliberately broken markup instead of
       crashing (the containment layer for FUTURE producer sites)."""
    import tui_app as A
    from textual.content import Content
    from rich.text import Text as RichText
    from tui_data import ChangeInfo, DetailRecord
    from tui_format import (external_badge_cell, external_rating_lines,
                            record_body_lines, verlauf_row_cells)

    H = "[/x][bold]BOOM[blink] ]a[ [/"
    STEM = "hostile__sweep"
    base_fb = len(A.MARKUP_FALLBACKS)

    rec = DetailRecord(
        insurer=H, tariff=H, stand=H,
        modules={"privat": {"included": True, "level": H, "hinweis": H}},
        coverage={"versicherungssumme": H, "selbstbeteiligung": H,
                  "wartezeit_monate": H, "geltungsbereich": H,
                  "vertragslaufzeit": H},
        leistungen=[H, "[/]"], ausschluesse=[H], besonderheiten=[H],
        beitrag={"monatlich_eur": 9.99, "quelle": H},
    )
    ext_entry = {"source": H, "badge": H, "sentiment": H, "scope": H,
                 "verdict": H, "stand": H, "url": 'https://x.example/["a"]'}

    # --- Part A: pure formatters, validated with their sink's real parser
    Content.from_markup("\n".join(record_body_lines(rec)))
    Content.from_markup("\n".join(external_rating_lines([ext_entry])))
    RichText.from_markup(external_badge_cell([ext_entry]))
    vrow = {"new_position": 1, "old_price": 0.0, "new_price": 2.0,
            "delta_price": 2.0, "delta_pos": 1, "is_new": False,
            "is_removed": False, "insurer": H, "product": H, "sb": H}
    for cell in verlauf_row_cells(vrow):
        RichText.from_markup(cell)

    # --- Part B: in-memory injection + live walk over every tab
    base = app._snapshot.rows[0]
    hrow = dataclasses.replace(
        base, position=998, insurer=H, product=H, tarifnote=H,
        selbstbeteiligung=H, wartezeit_per_modul={"privat": H},
        stem=STEM, has_detail=True, has_urls=True, key="hostile|sweep|1")
    app._snapshot.rows.append(hrow)
    app._details_by_stem[STEM] = rec
    hostile_docs = [{"doctype": H, "name": H, "url": 'https://x.example/["d"]'}]
    entry = {"stem": STEM, "insurer": H, "tariff": H, "docs": hostile_docs}
    app._doc_by_stem[STEM] = entry
    app._doc_index[STEM] = hostile_docs
    app._favorites.setdefault("favorites", []).append(
        {"insurer": H, "product": H, "sb": H, "tag": H, "stem": STEM})
    app._favorites["compare_stems"] = [STEM]  # in-memory only, never saved here
    app._favorite_notes[STEM] = H
    app._ext_ratings.setdefault("tariffs", {})[STEM] = [ext_entry]
    app._change_summary[STEM] = ChangeInfo(
        feature_changes=1, price_changes=1, last_change_date="2026-01-02",
        last_analysis_date="2026-01-02", first_seen_date="2026-01-01",
        feature_changelog=[("2026-01-01", "2026-01-02",
                            {"leistungen_added": [H], "leistungen_removed": [H]})],
        price_changelog=[{"date": "2026-01-02", "old_price": 1.0,
                          "new_price": 2.0, "delta": 1.0}],
        price_series=[{"date": "2026-01-01", "price": 1.0},
                      {"date": "2026-01-02", "price": 2.0}])

    for tab_key, table_id, ident in (
        ("x", "#market-table", "hostile|sweep|1"),
        ("y", "#fav-table", None),
        ("M", "#magic-table", STEM),
    ):
        await pilot.press(tab_key)
        await pilot.pause()
    app._populate_market_table()
    app._populate_favorites_table()
    app._populate_magic()
    app._populate_coverage()
    await pilot.pause()

    for table_id, prefix in (("#market-table", "hostile|sweep|1"),
                             ("#magic-table", STEM)):
        table = _table(app, table_id)
        rk = next((k for k in (app._market_rows if table_id == "#market-table"
                               else app._magic_rows) if k.startswith(prefix)), None)
        assert rk is not None, f"hostile row missing from {table_id}"
        table.move_cursor(row=table.get_row_index(rk))
        await pilot.pause()
        app._detail_visible = True
        app._show_detail()
        await pilot.pause()

    # Favorites band: highlight the hostile favorite (last row) + render.
    await pilot.press("y")
    await pilot.pause()
    fav_table = _table(app, "#fav-table")
    if fav_table.row_count:
        fav_table.move_cursor(row=fav_table.row_count - 1)
        await pilot.pause()
        app._detail_visible = True
        app._show_detail()
        await pilot.pause()

    # Vergleich matrix rendered with the hostile record as its only column.
    await pilot.press("v")
    await pilot.pause()

    # Verlauf detail markup for the hostile row (its table rows come from disk
    # snapshots, so validate the render path directly with the real parser).
    Content.from_markup(app._render_verlauf_detail(hrow))
    Content.from_markup(app._render_price_series(STEM))

    # Modal screens that interpolate manifest data.
    from tui_screens import ConfirmFetchScreen
    await app.push_screen(ConfirmFetchScreen(entry, model=H))
    await pilot.pause()
    await pilot.press("escape")
    await pilot.pause()

    assert len(A.MARKUP_FALLBACKS) == base_fb, (
        "a producer site relies on the containment guard — add the missing "
        f"_esc: {A.MARKUP_FALLBACKS[base_fb:]}")

    # --- Part C: the guard itself contains deliberately broken markup
    broken = "[/x]raw [unclosed"
    assert A.guard_content(broken).startswith("\\["), "guard_content no fallback"
    assert A.guard_cell(broken).startswith("\\["), "guard_cell no fallback"
    assert len(A.MARKUP_FALLBACKS) == base_fb + 2, "fallbacks not recorded"
    del A.MARKUP_FALLBACKS[base_fb:]  # don't leak into other cases


async def t_verlauf_stats(app, pilot) -> None:
    """The Verlauf tab renders the market-over-time header line and the per-stem
    Preisverlauf sparkline without markup errors (needs the repo's >=2 snapshots)."""
    if len(app._all_snapshots) < 2:
        return  # thin data: header shows the 2-snapshot hint instead — nothing to assert
    await pilot.press("l")
    await pilot.pause()  # _populate_verlauf ran -> header markup already survived render

    spark_chars = set("▁▂▃▄▅▆▇█")
    line = app._verlauf_market_line()
    assert "Markt über Zeit" in line and "Median" in line, f"market line malformed: {line!r}"
    assert spark_chars & set(line), f"no sparkline glyph in market line: {line!r}"

    stem = next(
        (s for s, ci in app._change_summary.items()
         if len([e for e in ci.price_series if e.get("price") is not None]) >= 2),
        None,
    )
    assert stem is not None, "no stem with >=2 priced snapshot points in change summary"
    section = app._render_price_series(stem)
    assert "Preisverlauf" in section and spark_chars & set(section), (
        f"price series section malformed for {stem}: {section!r}")

    # Full live path: highlight a Verlauf row and force the detail render.
    table = _table(app, "#verlauf-table")
    if table.row_count:
        table.move_cursor(row=0)
        await pilot.pause()
        app._detail_visible = True
        app._show_detail()
        await pilot.pause()  # reaching here without MarkupError is the assertion


async def t_table_less_tab_guards(app, pilot) -> None:
    """A row-cursor action must NOT fire on the last table tab's stale, now-invisible
    selection while a table-less tab (Benchmark) is active — the [D]-rmtrees-an-unseen-
    row class. Select a Market row, switch to Benchmark, and assert the active identity
    is gone and [D] opens no DeleteDataScreen."""
    await pilot.press("x")
    await pilot.pause()
    market = _table(app, "#market-table")
    market.move_cursor(row=0)
    await pilot.pause()
    assert app._active_identity() is not None, "Market row should have an identity"
    assert app._row_tab_active() is True, "Market is a row tab"

    await pilot.press("B")        # table-less Benchmark tab
    await pilot.pause()
    assert _active_tab(app) == "bench"
    assert app._row_tab_active() is False, "bench must not count as a row tab"
    assert app._active_identity() is None, (
        "identity must be None on a table-less tab — else [D]/[g] hit the stale row")
    base = len(app.screen_stack)
    await pilot.press("D")        # delete must no-op here, not push DeleteDataScreen
    assert await _wait_until(pilot, lambda: True) and len(app.screen_stack) == base, (
        "[D] on the Benchmark tab opened a modal — it targeted the invisible row")


async def t_benchmark_tab(app, pilot) -> None:
    """[B] switches to the Benchmark tab and the scorecard renders on the live path
    without a MarkupError; the populate replaced the placeholder with either real
    scorecard content or the explicit empty state."""
    await pilot.press("B")
    await pilot.pause()
    assert _active_tab(app) == "bench", f"[B] -> {_active_tab(app)!r}"
    content = app.query_one("#bench-content", Static)
    rendered = content.render()  # Textual 8.x dropped Static.renderable; render() is stable
    text = rendered.plain if hasattr(rendered, "plain") else str(rendered)
    assert "wird geladen" not in text, "bench content still the placeholder — populate did not run"
    assert ("Extraktionsqualität" in text or "Noch keine Benchmark-Daten" in text), (
        f"bench content neither scorecard nor empty state: {text[:80]!r}")


async def t_magic_tab(app, pilot) -> None:
    """[M] switches to the Magic Find tab; the table is populated and ranked by score
    descending; highlighting a row renders the score-breakdown band without a
    MarkupError, and the row count matches the in-memory score map (no row loss)."""
    await pilot.press("M")
    await pilot.pause()
    assert _active_tab(app) == "magic", f"[M] -> {_active_tab(app)!r}"
    table = _table(app, "#magic-table")
    assert table.row_count == len(app._magic_rows), (
        f"magic rows {table.row_count} != map {len(app._magic_rows)}")
    assert table.row_count > 0, "magic table empty — no analyzed tariffs?"
    totals = [s.total for s in app._magic_rows.values()]
    assert totals == sorted(totals, reverse=True), "magic rows not score-descending"
    assert len(table.columns) == 12, (
        f"magic table should have 12 columns incl. Ext + P/L, got {len(table.columns)}")
    table.move_cursor(row=0)
    await pilot.pause()
    assert app._active_row is not None, "magic top row has no representative snapshot row"
    app._detail_visible = True
    app._show_detail()
    await pilot.pause()
    content = app.query_one("#magic-detail-content", Static)
    rendered = content.render()
    text = rendered.plain if hasattr(rendered, "plain") else str(rendered)
    assert "Magic-Score" in text, f"magic detail not rendered: {text[:80]!r}"
    # info-only section is present and clearly separated from the scored dimensions
    assert "nicht gewertet" in text, f"magic detail missing info-only section: {text[:120]!r}"
    assert "Preis-Leistung" in text, f"magic detail missing P/L info: {text[:120]!r}"


async def t_magic_needs_toggle(app, pilot) -> None:
    """[P] toggles the Bedarf view on the Magic tab: re-ranks in place (no row loss,
    still score-descending), the header reflects the mode, and with neutral needs the
    re-rank is identical to the objective view (neutral == objective). A second [P]
    returns to the objective view. magic.NEEDS_PATH is patched to a nonexistent temp
    file so the test never depends on the user's real needs-weights.json."""
    import shutil
    import tempfile
    from pathlib import Path as _Path

    import magic as _magic

    orig = _magic.NEEDS_PATH
    tmpdir = tempfile.mkdtemp()
    # missing file -> load_needs falls back to the all-neutral default
    _magic.NEEDS_PATH = _Path(tmpdir) / "needs.json"
    try:
        await pilot.press("M")
        await pilot.pause()
        table = _table(app, "#magic-table")
        assert app._magic_needs_mode is False, "needs mode should start off"
        objective = [(rk, s.total) for rk, s in app._magic_rows.items()]

        await pilot.press("P")
        await pilot.pause()
        assert app._magic_needs_mode is True, "[P] did not enable needs mode"
        assert table.row_count == len(app._magic_rows) > 0, "row loss after needs toggle"
        totals = [s.total for s in app._magic_rows.values()]
        assert totals == sorted(totals, reverse=True), "needs-mode rows not score-descending"

        header = app.query_one("#magic-header")
        hr = header.render()
        htext = hr.plain if hasattr(hr, "plain") else str(hr)
        assert "Bedarf" in htext, f"header missing Bedarf marker: {htext[:80]!r}"

        neutral_now = [(rk, s.total) for rk, s in app._magic_rows.items()]
        assert neutral_now == objective, "neutral needs must reproduce the objective ranking"

        await pilot.press("P")
        await pilot.pause()
        assert app._magic_needs_mode is False, "[P] did not disable needs mode"
    finally:
        _magic.NEEDS_PATH = orig
        shutil.rmtree(tmpdir, ignore_errors=True)


async def t_external_ratings(app, pilot) -> None:
    """External test verdicts are display-only surfaces: whole-word insurer
    matching (no 'arag'-in-'oerag' leak), the Magic Ext column, and the
    'Externe Bewertungen' detail section render live without MarkupError."""
    from tui_data import external_ratings_for

    data = app._ext_ratings
    if not data.get("tariffs") and not data.get("insurers"):
        return  # sidecar absent: every surface degrades to a dash — nothing to assert

    # Data layer: rated stem gets tariff- AND insurer-level entries; an ÖRAG
    # product must get the insurer entry via the stem token but never ARAG's.
    arag = external_ratings_for("arag__komfort-2026", "ARAG", data)
    assert len(arag) >= 2, f"expected FT + FFF+ entries for arag komfort, got {arag}"
    oerag = external_ratings_for(
        "bavariadirekt__rundum-schutz-oerag", "BavariaDirekt / ÖRAG", data)
    badges = [e.get("badge") for e in oerag]
    assert "FFF+" in badges and not any("FT" in str(b) for b in badges), (
        f"oerag stem should carry only insurer-level FFF+, got {badges}")
    assert external_ratings_for("ergo__best", "Ergo", data) == [], (
        "unrated stem must yield no entries")
    # Alias: a product-identical variant inherits the base verdicts, marked via=
    if isinstance(data.get("tariff_aliases"), dict) and data["tariff_aliases"]:
        variant, base = next(iter(data["tariff_aliases"].items()))
        inherited = [e for e in external_ratings_for(variant, "", data)
                     if e.get("via") == base]
        assert inherited, f"alias {variant} did not inherit {base} verdicts"

    await pilot.press("M")
    await pilot.pause()
    table = _table(app, "#magic-table")
    labels = [str(c.label) for c in table.columns.values()]
    assert "Ext" in labels, f"Magic table missing Ext column: {labels}"

    # Live path: select a rated row, force the detail band, and require the
    # external section in the rendered text (also proves the markup survives).
    rk = app._magic_ident_to_rk.get("arag__komfort-2026")
    if rk is not None:
        idx = list(app._magic_rows).index(rk)
        table.move_cursor(row=idx)
        await pilot.pause()
        app._detail_visible = True
        app._show_detail()
        await pilot.pause()
        content = app.query_one("#magic-detail-content", Static)
        rendered = content.render()
        text = rendered.plain if hasattr(rendered, "plain") else str(rendered)
        assert "Externe Bewertungen" in text, (
            f"detail band missing external section: {text[:120]!r}")
        assert "kein Score-Input" in text, "external section must state display-only"

    # Blind-spot note: _market_notes surface in the Magic header.
    if data.get("_market_notes"):
        header = app.query_one("#magic-header")
        hr = header.render()
        htext = hr.plain if hasattr(hr, "plain") else str(hr)
        assert "außerhalb CHECK24" in htext, (
            f"header missing blind-spot note: {htext!r}")


async def t_needs_editor(app, pilot) -> None:
    """[W] opens the Bedarf-weights editor; setting a non-neutral level and saving
    persists to needs-weights.json, flips Magic into Bedarf mode and re-ranks without
    error; reopening and escaping cancels. magic.NEEDS_PATH is patched to a temp file so
    the real config is never touched."""
    import json as _json
    import shutil
    import tempfile
    from pathlib import Path as _Path

    import magic as _magic
    from tui_screens import NeedsEditorScreen

    orig = _magic.NEEDS_PATH
    tmpdir = tempfile.mkdtemp()
    tmp = _Path(tmpdir) / "needs.json"
    _magic.NEEDS_PATH = tmp
    try:
        await pilot.press("M")
        await pilot.pause()
        assert app._magic_needs_mode is False, "needs mode should start off"

        base = len(app.screen_stack)
        await pilot.press("W")
        assert await _wait_until(pilot, lambda: len(app.screen_stack) > base), (
            "[W] did not open the needs editor")
        assert isinstance(app.screen, NeedsEditorScreen), (
            f"[W] opened {type(app.screen).__name__}, want NeedsEditorScreen")

        await pilot.press("3")   # set the first Baustein to level 3
        await pilot.press("s")   # save
        assert await _wait_until(pilot, lambda: len(app.screen_stack) == base), (
            "editor did not close on save")
        assert tmp.is_file(), "needs-weights.json was not written"
        saved = _json.loads(tmp.read_text(encoding="utf-8"))
        assert any(v == 3 for k, v in saved.items() if k != "_comment"), (
            f"no level-3 weight persisted: {saved}")
        assert app._magic_needs_mode is True, (
            "a non-neutral save should switch Bedarf mode on")

        await pilot.press("W")   # reopen, then cancel
        assert await _wait_until(pilot, lambda: isinstance(app.screen, NeedsEditorScreen))
        await pilot.press("escape")
        assert await _wait_until(
            pilot, lambda: not isinstance(app.screen, NeedsEditorScreen)), (
            "escape did not cancel the editor")
    finally:
        _magic.NEEDS_PATH = orig
        shutil.rmtree(tmpdir, ignore_errors=True)


async def t_magic_scan_modal(app, pilot) -> None:
    """[F] runs the deep-scan funnel's candidate selection and, when top-pool_k
    products are still un-analyzed, opens the MagicScanScreen confirm — a long, paid,
    market-wide op that must NEVER auto-fire. Escape cancels it and starts no pipeline.
    Playwright/LLM are not exercised here (sandbox blocks them); this pins the wiring
    up to the confirm gate and the candidate-selection branch."""
    import magic as _magic
    from tui_screens import MagicScanScreen

    weights = _magic.load_weights()
    pre = _magic.prescore(app._snapshot.rows)
    selected, _dropped = _magic.select_candidates(pre, weights.pool_k)
    missing = [p for p in selected if not p.has_detail]

    base = len(app.screen_stack)
    await pilot.press("F")
    if missing:
        assert await _wait_until(pilot, lambda: len(app.screen_stack) > base), (
            "[F] did not open the scan confirm despite un-analyzed candidates")
        assert isinstance(app.screen, MagicScanScreen), (
            f"[F] opened {type(app.screen).__name__}, want MagicScanScreen")
        await pilot.press("escape")
        assert await _wait_until(pilot, lambda: len(app.screen_stack) == base), (
            "scan confirm did not close on escape")
        assert app._pipeline_running is False, "cancel must not start the pipeline"
    else:
        # Everything in the top pool is already analyzed -> notify only, no modal.
        assert await _wait_until(pilot, lambda: True) and len(app.screen_stack) == base, (
            "[F] opened a modal even though no candidates are missing")
        assert app._pipeline_running is False


async def t_update_all_modal(app, pilot) -> None:
    """[U] opens the UpdateAllScreen confirm (full refresh must NEVER auto-fire)
    with extract flags derived from the record provenance; Escape cancels without
    claiming the pipeline slot. Subprocesses are not exercised here."""
    from tui_data import dominant_provenance
    from tui_screens import UpdateAllScreen

    model, filter_on, repeat = dominant_provenance()
    assert isinstance(filter_on, bool) and repeat >= 1, (
        f"provenance malformed: {model!r}, {filter_on!r}, {repeat!r}")

    base = len(app.screen_stack)
    await pilot.press("U")
    assert await _wait_until(pilot, lambda: len(app.screen_stack) > base), (
        "[U] did not open the update-all confirm")
    assert isinstance(app.screen, UpdateAllScreen), (
        f"[U] opened {type(app.screen).__name__}, want UpdateAllScreen")
    await pilot.press("escape")
    assert await _wait_until(pilot, lambda: len(app.screen_stack) == base), (
        "update-all confirm did not close on escape")
    assert app._pipeline_running is False, "cancel must not start the pipeline"


async def t_pipeline_single_flight(app, pilot) -> None:
    """The analyze slot is single-flight: a second claim while one is held is refused,
    so two confirm callbacks (stacked confirm modals — App bindings keep firing under a
    ModalScreen) can never launch two racing pipelines. Pins the atomic-claim fix."""
    assert app._pipeline_running is False, "slot not free at start"
    assert app._claim_pipeline() is True, "first claim should succeed"
    assert app._pipeline_running is True, "claim did not set the flag"
    assert app._claim_pipeline() is False, "second claim must be refused while held"
    assert app._pipeline_busy() is True, "_pipeline_busy disagrees the slot is taken"
    app._pipeline_running = False   # release (no worker was actually started here)
    assert app._claim_pipeline() is True, "claim should succeed again after release"
    app._pipeline_running = False


async def t_pipeline_status_line(app, pilot) -> None:
    """The live pipeline status line renders progress markup into #status-bar
    (bottom, above the Footer) and is restored to the idle hint on reload — so a
    failed stage stays visible until the next reload while success clears it. The
    [N/M] stage counter must render literally, not crash the markup parser."""
    app._set_pipeline_status(
        "[yellow]⏳ [1/3] Harvest+Download[/yellow] [dim]scraping …[/dim]"
    )
    await pilot.pause()
    bar = app.query_one("#status-bar", Label)
    txt = str(bar.render())
    assert "Harvest+Download" in txt, f"status line did not render stage: {txt!r}"
    assert "[1/3]" in txt, f"stage counter not literal: {txt!r}"
    app._reload_all()   # success path restores the idle hint
    await pilot.pause()
    txt2 = str(app.query_one("#status-bar", Label).render())
    assert "Reload" in txt2, f"status line not restored after reload: {txt2!r}"


async def t_splash_and_loader(app, pilot) -> None:
    """Boot splash: never auto-pushed headless; frames advance on the timer; any
    key skips; a fast full playback self-dismisses without a double-dismiss
    crash. Loader bar: animates in front of the pipeline status while
    _pipeline_running and disappears from the final line when the run ends."""
    import tui_anim
    from tui_screens import SplashScreen

    assert not isinstance(app.screen, SplashScreen), "splash leaked into headless run"

    frames = tui_anim.splash_frames("1")
    await app.push_screen(SplashScreen(frames))
    await pilot.pause()
    assert isinstance(app.screen, SplashScreen)
    idx0 = app.screen._idx
    await asyncio.sleep(0.5)
    await pilot.pause()
    assert app.screen._idx > idx0, "splash frames did not advance"
    await pilot.press("space")
    await pilot.pause()
    assert not isinstance(app.screen, SplashScreen), "key did not skip splash"

    # ~100 frames since the settle phase; 5ms nominal + timer overhead needs
    # more than the old 0.6s to play through.
    await app.push_screen(SplashScreen(frames, interval=0.005))
    await asyncio.sleep(1.5)
    await pilot.pause()
    assert not isinstance(app.screen, SplashScreen), "splash did not self-dismiss"

    app._pipeline_running = True
    try:
        app._set_pipeline_status("[yellow]⏳ [2/4] Extract[/yellow]")
        before = str(app.query_one("#status-bar", Label).render())
        app._animate_pipeline_status()
        running = str(app.query_one("#status-bar", Label).render())
        assert "⚡" in running and "Extract" in running, running
        assert before != running, "loader bar did not animate"

        # fat centered overlay: shown, on top at screen center, click-through
        # at the edges (the layer container is visibility:hidden)
        await pilot.pause()
        layer = app.query_one("#loader-layer")
        box = app.query_one("#loader-overlay", Static)
        assert layer.display, "loader overlay not shown while pipeline runs"
        assert "⚡" in str(box.render()) and "Extract" in str(box.render())
        cx, cy = app.size.width // 2, app.size.height // 2
        center_w, _ = app.get_widget_at(cx, cy)
        assert center_w.id == "loader-overlay", f"overlay not on top: {center_w!r}"
        corner_w, _ = app.get_widget_at(1, 4)
        assert corner_w.id not in ("loader-layer", "loader-overlay"), (
            f"loader layer swallows clicks outside the box: {corner_w!r}")
    finally:
        app._pipeline_running = False
    app._animate_pipeline_status()
    await pilot.pause()
    final = str(app.query_one("#status-bar", Label).render())
    assert "⚡" not in final and "Extract" in final, final
    assert not app.query_one("#loader-layer").display, "overlay not hidden after run"


async def t_vertical_switch(app, pilot) -> None:
    """[S] opens the vertical selector; switching to an empty fixture vertical swaps
    the Market tab to the new vertical's (empty) data and the header label; switching
    back restores the Rechtsschutz rows unchanged. Fixture: a minimal schema plus a
    registry entry injected into the cached registry, removed afterwards."""
    import json
    import os
    import shutil

    import _modules
    import _vertical
    from textual.widgets import OptionList

    fixture = "probesparte"
    schema_dir = _vertical.ROOT / "schema" / fixture
    assert not schema_dir.exists(), f"fixture dir {schema_dir} already exists"
    schema_dir.mkdir(parents=True)
    (schema_dir / "tariff.schema.json").write_text(json.dumps(
        {"properties": {"modules": {"properties": {"grundschutz": {}}}}}
    ), encoding="utf-8")
    reg = _vertical.registry()
    assert fixture not in reg["verticals"]
    reg["verticals"][fixture] = {"label": "Probesparte", "host": "https://example.invalid",
                                 "funnel_path": "/", "status": "experimental"}

    async def pick(name: str) -> None:
        base = len(app.screen_stack)
        await pilot.press("S")
        assert await _wait_until(pilot, lambda: len(app.screen_stack) > base), (
            "[S] did not open the vertical selector")
        lst = app.screen_stack[-1].query_one("#vertical-list", OptionList)
        idx = next(i for i in range(lst.option_count)
                   if lst.get_option_at_index(i).id == name)
        lst.highlighted = idx
        await pilot.press("enter")
        assert await _wait_until(pilot, lambda: _vertical.active() == name), (
            f"switch to {name} did not apply")
        await pilot.pause()

    try:
        assert _vertical.active() == "rechtsschutz"
        base_rows = len(app._market_rows)
        assert base_rows > 0, "no RS market rows to begin with"

        await pick(fixture)
        assert len(app._market_rows) == 0, (
            f"fixture market should be empty, got {len(app._market_rows)}")
        assert "Probesparte" in str(app.sub_title), app.sub_title
        assert app._snapshot is None, "RS snapshot leaked into the fixture vertical"
        assert _modules.module_keys() == ("grundschutz",), _modules.module_keys()

        await pick("rechtsschutz")
        assert len(app._market_rows) == base_rows, (
            f"RS rows not restored: {len(app._market_rows)} != {base_rows}")
        assert "Rechtsschutz" in str(app.sub_title), app.sub_title
        assert len(_modules.module_keys()) == 8, _modules.module_keys()
    finally:
        reg["verticals"].pop(fixture, None)
        os.environ.pop("CHECK0R_VERTICAL", None)
        _modules.reset_cache()
        shutil.rmtree(schema_dir, ignore_errors=True)


CASES = [
    ("boot_and_tables", t_boot_and_tables),
    ("pipeline_single_flight", t_pipeline_single_flight),
    ("pipeline_status_line", t_pipeline_status_line),
    ("tab_shortcuts", t_tab_shortcuts),
    ("tab_cycle", t_tab_cycle),
    ("contextual_footer", t_contextual_footer),
    ("benchmark_tab", t_benchmark_tab),
    ("magic_tab", t_magic_tab),
    ("magic_needs_toggle", t_magic_needs_toggle),
    ("external_ratings", t_external_ratings),
    ("needs_editor", t_needs_editor),
    ("magic_scan_modal", t_magic_scan_modal),
    ("update_all_modal", t_update_all_modal),
    ("table_less_tab_guards", t_table_less_tab_guards),
    ("cross_tab_roundtrip", t_cross_tab_roundtrip),
    ("cross_tab_held_absent", t_cross_tab_held_absent),
    ("detail_toggle", t_detail_toggle),
    ("enter_detail_no_browser", t_enter_detail_no_browser),
    ("market_sort_toggle", t_market_sort_toggle),
    ("market_sort_bew", t_market_sort_bew),
    ("filter_debounce", t_filter_debounce),
    ("help_modal", t_help_modal),
    ("markup_hostile_nav", t_markup_hostile_nav),
    ("hostile_data_sweep", t_hostile_data_sweep),
    ("verlauf_stats", t_verlauf_stats),
    ("splash_and_loader", t_splash_and_loader),
    ("vertical_switch", t_vertical_switch),
]


async def _run_case(fn) -> tuple[bool, str]:
    app = CheckApp(snapshot_path=None)
    try:
        async with app.run_test(size=TEST_SIZE) as pilot:
            await pilot.pause()
            await fn(app, pilot)
        return True, ""
    except Exception as e:  # noqa: BLE001 — report any failure as a clean FAIL
        return False, f"{type(e).__name__}: {e}\n" + traceback.format_exc()


async def _main() -> int:
    print("=== check0r3000 TUI live runtime test ===")
    passed = 0
    failures: list[str] = []
    for name, fn in CASES:
        ok, detail = await _run_case(fn)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if ok:
            passed += 1
        else:
            failures.append(f"{name}:\n{detail}")
    print(f"\n{passed}/{len(CASES)} passed")
    if failures:
        print("\n--- failures ---")
        for f in failures:
            print(f)
        print("=== TUI test FAILED ===")
        return 1
    print("=== TUI test PASSED ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
