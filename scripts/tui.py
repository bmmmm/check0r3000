#!/usr/bin/env -S uv run
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
background, after a confirm; [G] runs the same analysis WITHOUT the download when
the source PDFs are already in data/raw/<stem>/. The extract model defaults to
"claude"; override with the CHECK0R_ANALYZE_MODEL env var. Tariffs whose URLs were
never harvested point you back to the browser "Tarifdetails" step. The Vergleich
tab [v] shows an across-tariff coverage comparison (modules, coverage, and
taxonomy-aligned Leistungen/Ausschlüsse) built from the analyzed records: [w]
toggles the verbatim per-insurer wording (compact ↔ verbose), [t] opens a modal with
the full untruncated wording per category across all tariffs, [c] hides/shows the
selected tariff in the comparison, and [o] opens a tariff's source documents online
or as the local PDFs. Press [b] to view the read-only CHECK24 query URL, or [e] to
edit the query levers (provider, modules, birthdate, zipcode, sort, …) in place and
save them back to config/check24-profile.json.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# coverage_taxonomy / tui_data / tui_format live alongside this script;
# make scripts/ importable whether this module is reached as a file or via
# `uv run`, then import the siblings.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tui_data import (  # noqa: E402
    _load_detail,
    _raw_dir_for_stem,
    load_all_details,
    run_selftest,
)
from tui_format import _col_label  # noqa: E402
from textual.widgets import DataTable, TabbedContent  # noqa: E402

from tui_app import ANALYZE_MODEL, CheckApp  # noqa: E402
from tui_screens import (  # noqa: E402
    CompareManagerScreen,
    CompareTextScreen,
    ConfirmFetchScreen,
    OpenSourceScreen,
)


def _launch_app(snapshot_path: Path | None, screenshot_dir: Path | None = None) -> None:
    """Run the interactive app, or render the screenshot set and exit when
    screenshot_dir is given."""
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
                # Prefer an analyzed favorite so the shot shows the full record body
                # (the band that used to read "Module oben" and render nothing).
                first_fav = None
                for _k, (row, fav) in app._fav_rows.items():
                    if row is not None and _load_detail(row.insurer, row.product):
                        first_fav = (row, fav)
                        break
                if first_fav is None:
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
                # Scroll the band to the record body (Module/Deckung/Leistungen) — the
                # part that used to be a dead "Module oben" pointer — and capture it.
                try:
                    band = app.query_one("#fav-detail")
                    band.scroll_end(animate=False)
                    await pilot.pause()
                    app.save_screenshot(filename="favorites-body.svg", path=str(screenshot_dir))
                    band.scroll_home(animate=False)
                except Exception:
                    pass

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

                # --- Vergleich (coverage comparison) — compact, then verbose ---
                tabs.active = "diff"
                await pilot.pause()
                app.save_screenshot(filename="vergleich.svg", path=str(screenshot_dir))
                app._compare_verbose = True
                app._populate_coverage()
                await pilot.pause()
                app.save_screenshot(filename="vergleich-verbose.svg", path=str(screenshot_dir))
                app._compare_verbose = False
                app._populate_coverage()

                # --- Vergleich full-text modal ([t]) — full wording per category ---
                ft_entries, ft_ncols = app._fulltext_entries()
                if ft_entries:
                    await app.push_screen(CompareTextScreen(ft_entries, ft_ncols))
                    await pilot.pause()
                    app.save_screenshot(filename="fulltext.svg", path=str(screenshot_dir))
                    app.pop_screen()
                    await pilot.pause()

                # --- Vergleich manager modal ([c]) — toggle/clear/include tariffs ---
                mgr_details = load_all_details()
                if mgr_details:
                    mgr_ref = app._favorites.get("reference_stem")
                    mgr_stems = sorted(
                        ((stem, _col_label(stem)) for stem, _ in mgr_details),
                        key=lambda sl: (sl[0] != mgr_ref, sl[0]),
                    )
                    mgr_included = {
                        s for s, _ in mgr_details if s in set(app._compare_stems())
                    }
                    await app.push_screen(
                        CompareManagerScreen(mgr_stems, mgr_included, mgr_ref)
                    )
                    await pilot.pause()
                    app.save_screenshot(
                        filename="compare-manager.svg", path=str(screenshot_dir)
                    )
                    app.pop_screen()
                    await pilot.pause()

                # --- Modals over the market tab ([o] open-source, then [g] confirm) ---
                tabs.active = "market"
                await pilot.pause()
                osrow = next(
                    (r for r in app._snapshot.rows
                     if r.stem and app._doc_by_stem.get(r.stem)),
                    None,
                ) if app._snapshot else None
                if osrow is not None:
                    entry = app._doc_by_stem.get(osrow.stem) or {}
                    docs = entry.get("docs", [])
                    raw = _raw_dir_for_stem(osrow.stem)
                    n_pdfs = len(list(raw.glob("*.pdf"))) if raw.is_dir() else 0
                    await app.push_screen(
                        OpenSourceScreen(
                            f"{osrow.insurer} {osrow.product}", docs,
                            any(d.get("url") for d in docs), n_pdfs, osrow.stem,
                        )
                    )
                    await pilot.pause()
                    app.save_screenshot(filename="open.svg", path=str(screenshot_dir))
                    app.pop_screen()
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
            "Saved screenshots (favorites/market/market-detail/vergleich/"
            "vergleich-verbose/fulltext/compare-manager/open/confirm .svg) to "
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
