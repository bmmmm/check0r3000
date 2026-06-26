#!/usr/bin/env -S uv run --script
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

sys.path.insert(0, str(Path(__file__).resolve().parent))

from textual.widgets import DataTable, Static, TabbedContent  # noqa: E402

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
    for want in ("market", "diff", "verlauf", "bench", "favorites"):
        await pilot.press("tab")
        await pilot.pause()
        assert _active_tab(app) == want, f"tab -> {_active_tab(app)!r}, want {want!r}"
    await pilot.press("shift+tab")
    await pilot.pause()
    assert _active_tab(app) == "bench", f"shift+tab -> {_active_tab(app)!r}"


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


CASES = [
    ("boot_and_tables", t_boot_and_tables),
    ("tab_shortcuts", t_tab_shortcuts),
    ("tab_cycle", t_tab_cycle),
    ("benchmark_tab", t_benchmark_tab),
    ("cross_tab_roundtrip", t_cross_tab_roundtrip),
    ("cross_tab_held_absent", t_cross_tab_held_absent),
    ("detail_toggle", t_detail_toggle),
    ("filter_debounce", t_filter_debounce),
    ("help_modal", t_help_modal),
    ("markup_hostile_nav", t_markup_hostile_nav),
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
