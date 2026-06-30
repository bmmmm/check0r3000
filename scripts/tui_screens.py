"""check0r3000 — modal screen widgets.

Textual-bound leaf module: the nine ModalScreen subclasses ([g] confirm,
delete-data, query-URL, query-edit, save-confirm, open-source, full-text
compare, compare-manager, help). They reference no app or helper names — only
Textual plus two sibling formatters — so they import cleanly without CheckApp
(no circular import). CSS for each lives in tui.tcss, matched by class name."""

from __future__ import annotations

import sys
from pathlib import Path

# coverage_taxonomy / tui_data / tui_format live alongside this script;
# make scripts/ importable whether this module is reached as a file or via
# `uv run`, then import the siblings.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from textual.app import ComposeResult  # noqa: E402
from textual.binding import Binding  # noqa: E402
from textual.containers import (  # noqa: E402
    Container,
    Horizontal,
    ScrollableContainer,
)
from textual.css.query import NoMatches  # noqa: E402
from textual.screen import ModalScreen  # noqa: E402
from textual.widgets import (  # noqa: E402
    Button,
    Input,
    Label,
    OptionList,
    Static,
    Switch,
    TextArea,
)
from textual.widgets.option_list import Option  # noqa: E402

from tui_data import _DOCTYPE_SHORT  # noqa: E402
from tui_format import _esc  # noqa: E402


class ConfirmFetchScreen(ModalScreen[bool]):
    """Deliberate gate before the analyze pipeline. With skip_download the source
        PDFs are already on disk and only ingest → extract runs (still a paid model
        call, so the confirm stays). Returns True on confirm, False on cancel."""

    BINDINGS = [
        Binding("enter", "confirm", "Analyze"),
        Binding("y", "confirm", "Yes"),
        Binding("escape", "cancel", "Cancel"),
        Binding("n", "cancel", "No"),
    ]

    def __init__(self, entry: dict, model: str, skip_download: bool = False,
                 harvest: bool = False) -> None:
        super().__init__()
        self._entry = entry
        self._model = model
        self._skip_download = skip_download
        self._harvest = harvest

    def compose(self) -> ComposeResult:
        e = self._entry
        docs = e.get("docs", [])
        if self._harvest:
            lines = [
                f"[bold]{_esc(e.get('insurer', ''))} — {_esc(e.get('tariff', ''))}[/bold]",
                "",
                "[bold]Live-Harvest[/bold] — lädt die CHECK24-Ergebnisseite headless "
                "([dim]~30–60s[/dim]), liest die Tarifdetails-URLs,",
                "lädt die PDFs nach [cyan]data/raw/[/cyan], dann ingest → extract  "
                f"[dim](Modell: {self._model})[/dim].",
                "[dim]Headless-Browser + Drittanbieter-Copyright + ein Modell-Call.[/dim]",
                "",
                "[bold]\\[↵/y][/bold] Harvest + Analyse     [bold]\\[Esc/n][/bold] Abbrechen",
            ]
        elif self._skip_download:
            lines = [
                f"[bold]{_esc(e.get('insurer', ''))} — {_esc(e.get('tariff', ''))}[/bold]",
                "",
                "[bold]Analyse ohne Download[/bold] — PDFs liegen lokal "
                f"([cyan]data/raw/{_esc(e.get('stem', '').replace('__', '/'))}/[/cyan]).",
                f"ingest → extract  [dim](Modell: {self._model})[/dim]",
                "[dim]Extraktion ist ein Modell-Call.[/dim]",
                "",
                "[bold]\\[↵/y][/bold] Analysieren     [bold]\\[Esc/n][/bold] Abbrechen",
            ]
        else:
            lines = [
                f"[bold]{_esc(e.get('insurer', ''))} — {_esc(e.get('tariff', ''))}[/bold]",
                "",
                f"Download von [cyan]rechtsschutz.check24.de[/cyan]: "
                f"[bold]{len(docs)}[/bold] PDF(s)",
            ]
            for dd in docs:
                lbl = _DOCTYPE_SHORT.get(dd.get("doctype", ""), dd.get("doctype", ""))
                lines.append(f"  • [cyan]{lbl:<6}[/cyan] {_esc((dd.get('file') or '')[:48])}")
            lines += [
                "",
                f"dann: ingest → extract  [dim](Modell: {self._model})[/dim]",
                "[dim]Drittanbieter-Copyright — nur für den Eigengebrauch.[/dim]",
                "",
                "[bold]\\[↵/y][/bold] Download + Analyse     [bold]\\[Esc/n][/bold] Abbrechen",
            ]
        yield Container(Static("\n".join(lines)), id="confirm-box")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

class MagicScanScreen(ModalScreen[bool]):
    """Deliberate gate before the Magic deep-scan: it harvests (headless browser),
        downloads and analyzes the top-prescored candidates still missing a record — a
        long, paid, market-wide operation, so it lists exactly what it will analyze and
        announces any equally-scored tariffs the pool_k cap left out (no silent cap).
        Returns True on confirm, False on cancel."""

    BINDINGS = [
        Binding("enter", "confirm", "Scan"),
        Binding("y", "confirm", "Yes"),
        Binding("escape", "cancel", "Cancel"),
        Binding("n", "cancel", "No"),
    ]

    def __init__(self, candidates: list[tuple[str, str]], n_dropped: int,
                 n_selected: int, model: str) -> None:
        super().__init__()
        self._candidates = candidates
        self._n_dropped = n_dropped
        self._n_selected = n_selected
        self._model = model

    def compose(self) -> ComposeResult:
        n = len(self._candidates)
        lines = [
            "[bold]✨ Magic Markt-Scan[/bold]",
            "",
            f"Top {self._n_selected} der Vorab-Bewertung — davon [bold]{n}[/bold] noch "
            "ohne Analyse. Diese werden",
            "live geharvestet ([dim]headless Browser[/dim]), geladen und analysiert  "
            f"[dim](Modell: {self._model})[/dim]:",
        ]
        shown = self._candidates[:15]
        for ins, prod in shown:
            lines.append(f"  • [cyan]{_esc(ins)}[/cyan] — {_esc(prod)}")
        if n > len(shown):
            lines.append(f"  [dim]… und {n - len(shown)} weitere[/dim]")
        if self._n_dropped:
            lines += [
                "",
                f"[yellow]⚠ {self._n_dropped} weitere(r) Tarif(e) mit gleichem "
                f"Vorab-Score liegen außerhalb des Top-{self._n_selected}-Pools[/yellow]",
                "[dim]  pool_k in config/magic-weights.json erhöhen, um sie "
                "mitzunehmen.[/dim]",
            ]
        lines += [
            "",
            "[dim]Ein Headless-Browser-Lauf + ein Modell-Call je Stufe; "
            "Drittanbieter-Copyright — nur Eigengebrauch.[/dim]",
            "",
            "[bold]\\[↵/y][/bold] Scan starten     [bold]\\[Esc/n][/bold] Abbrechen",
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
        i, _, t = (_esc(p) for p in self._stem.partition("__"))
        stem_e = _esc(self._stem)
        unfav = "" if self._is_fav else "   [dim](nicht in Favoriten)[/dim]"
        lines = [
            f"[bold]Daten löschen — {_esc(self._label)}[/bold]",
            f"[dim]stem: {stem_e}[/dim]",
            "[dim]Irreversibel.[/dim]",
            "",
            "[bold]\\[1][/bold] Nur Analyse-Records",
            f"     [dim]out/tariffs|enriched/{stem_e}.json — per \\[g] neu erzeugbar[/dim]",
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

class QueryEditScreen(ModalScreen[dict[str, str] | None]):
    """Edit the curated CHECK24 query levers in place. Pre-filled from the parsed
        query; returns {lever_key: new_value} for the levers the user changed-or-kept,
        or None on cancel. The app applies these via check24_query.set_param so every
        OTHER (uncurated / repeated / unknown) query param survives the round-trip
        verbatim. discounts stays read-only (a JSON blob); it is only displayed."""

    BINDINGS = [
        Binding("ctrl+s", "save", "Speichern"),
        Binding("escape", "cancel", "Abbrechen"),
    ]

    # (lever_key, German label) for the free-text Input fields, in display order.
    TEXT_FIELDS = [
        ("provider_filter", "Versicherer-ID (provider_filter)"),
        ("tariff_position", "Tarif-Position"),
        ("maritalstatus", "Familienstand"),
        ("birthdate", "Geburtsdatum (TT.MM.JJJJ)"),
        ("zipcode", "PLZ"),
        ("employmentstatus", "Beschäftigung"),
        ("employmentstatus_partner", "Beschäftigung Partner"),
        ("costsharing", "Selbstbeteiligung"),
        ("sortfield", "Sortierfeld"),
        ("sortorder", "Sortierrichtung"),
    ]
    # (lever_key, German label) for the Switch (yes/no) fields, in display order.
    SWITCH_FIELDS = [
        ("module_priv", "Modul Privat"),
        ("module_job", "Modul Beruf"),
        ("module_traffic", "Modul Verkehr"),
        ("module_living", "Modul Wohnen"),
        ("module_rental", "Modul Vermietung"),
        ("stiftung_warentest", "Stiftung Warentest"),
    ]

    def __init__(self, values: dict[str, str], provider_name_fn,
                 discounts: list[str], is_example: bool) -> None:
        super().__init__()
        # values: the current value of every curated lever (missing -> "").
        self._values = dict(values)
        self._provider_name_fn = provider_name_fn
        self._discounts = discounts
        self._is_example = is_example

    def _provider_hint(self) -> str:
        pid = (self._values.get("provider_filter") or "").strip()
        if not pid:
            return "[dim]alle Versicherer[/dim]"
        name = self._provider_name_fn(pid)
        return f"[cyan]{_esc(name)}[/cyan]" if name else "[yellow]unbekannte ID[/yellow]"

    def compose(self) -> ComposeResult:
        with Container(id="query-edit-box"):
            yield Static(
                "[bold]CHECK24-Suche bearbeiten[/bold]   "
                "[dim]Tab/↑↓ Feld wählen · \\[Ctrl+S] speichern · "
                "\\[Esc] abbrechen[/dim]",
                id="query-edit-head",
            )
            if self._is_example:
                yield Static(
                    "[yellow]! Nur das Beispielprofil (Fake-Daten) geladen — "
                    "Speichern legt config/check24-profile.json mit deinen "
                    "echten Werten an.[/yellow]",
                    id="query-edit-warn",
                )
            with ScrollableContainer(id="query-edit-body"):
                for key, label in self.TEXT_FIELDS:
                    hint = ""
                    if key == "provider_filter":
                        hint = f"  → {self._provider_hint()}"
                    yield Static(f"[bold]{_esc(label)}[/bold]{hint}")
                    yield Input(
                        value=self._values.get(key, ""),
                        id=f"qe-{key}",
                        classes="qe-input",
                    )
                for key, label in self.SWITCH_FIELDS:
                    with Horizontal(classes="qe-switch-row"):
                        yield Switch(
                            value=(self._values.get(key) == "yes"),
                            id=f"qe-{key}",
                        )
                        yield Label(label, classes="qe-switch-label")
                blob = ", ".join(self._discounts) or "(keine)"
                yield Static(
                    f"[dim]Rabatte (nur Anzeige): {_esc(blob)}[/dim]",
                    classes="qe-readonly",
                )
            with Horizontal(id="query-edit-buttons"):
                yield Button("Speichern", variant="success", id="qe-save")
                yield Button("Abbrechen", variant="error", id="qe-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "qe-save":
            self.action_save()
        elif event.button.id == "qe-cancel":
            self.action_cancel()

    def _collect(self) -> dict[str, str]:
        """Read every editable widget back into {lever_key: value}."""
        out: dict[str, str] = {}
        for key, _ in self.TEXT_FIELDS:
            out[key] = self.query_one(f"#qe-{key}", Input).value.strip()
        for key, _ in self.SWITCH_FIELDS:
            out[key] = "yes" if self.query_one(f"#qe-{key}", Switch).value else "no"
        return out

    def _validate(self, vals: dict[str, str]) -> str | None:
        """Return an actionable German error message, or None if all fields pass."""
        import re

        bd = vals.get("birthdate", "")
        if bd and not re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", bd):
            return ("Geburtsdatum: Format TT.MM.JJJJ erwartet "
                    f"(z. B. 01.01.1990), nicht {bd!r}.")
        zc = vals.get("zipcode", "")
        if zc and not re.fullmatch(r"\d{5}", zc):
            return f"PLZ: 5 Ziffern erwartet, nicht {zc!r}."
        cs = vals.get("costsharing", "")
        if cs and not re.fullmatch(r"\d+", cs):
            return f"Selbstbeteiligung: nur Ziffern erwartet, nicht {cs!r}."
        pf = vals.get("provider_filter", "")
        if pf and not re.fullmatch(r"\d+", pf):
            return ("Versicherer-ID: nur Ziffern erwartet (leer = alle "
                    f"Versicherer), nicht {pf!r}.")
        tp = vals.get("tariff_position", "")
        if tp and not re.fullmatch(r"\d+", tp):
            return f"Tarif-Position: nur Ziffern erwartet, nicht {tp!r}."
        so = vals.get("sortorder", "")
        if so and so not in ("asc", "desc"):
            return f"Sortierrichtung: 'asc' oder 'desc' erwartet, nicht {so!r}."
        return None

    def action_save(self) -> None:
        vals = self._collect()
        err = self._validate(vals)
        if err is not None:
            self.notify(err, severity="error", timeout=8)
            return
        self.dismiss(vals)

    def action_cancel(self) -> None:
        self.dismiss(None)

class QuerySaveConfirmScreen(ModalScreen[bool]):
    """Confirm gate before overwriting config/check24-profile.json with the edited
        query. Shows which levers changed so the write is deliberate. Returns True on
        confirm, False on cancel."""

    BINDINGS = [
        Binding("enter", "confirm", "Speichern"),
        Binding("y", "confirm", "Ja"),
        Binding("escape", "cancel", "Abbrechen"),
        Binding("n", "cancel", "Nein"),
    ]

    def __init__(self, changes: list[tuple[str, str, str]], is_example: bool) -> None:
        super().__init__()
        # changes: (lever_key, old_value, new_value) for changed levers only.
        self._changes = changes
        self._is_example = is_example

    def compose(self) -> ComposeResult:
        lines = ["[bold]Suche speichern?[/bold]", ""]
        if self._is_example:
            lines += [
                "[yellow]Legt config/check24-profile.json an (gitignored, deine "
                "echten Werte).[/yellow]",
                "",
            ]
        else:
            lines += [
                "[dim]Überschreibt config/check24-profile.json (atomar).[/dim]",
                "",
            ]
        if self._changes:
            lines.append("[underline]Geänderte Levers[/underline]")
            for key, old, new in self._changes:
                lines.append(
                    f"  [cyan]{_esc(key)}[/cyan]: "
                    f"[dim]{_esc(old) or '(leer)'}[/dim] → [bold]{_esc(new) or '(leer)'}[/bold]"
                )
        else:
            lines.append("[dim]Keine Lever-Änderung — Query bleibt gleich.[/dim]")
        lines += [
            "",
            "[bold]\\[↵/y][/bold] Speichern     [bold]\\[Esc/n][/bold] Abbrechen",
        ]
        yield Container(Static("\n".join(lines)), id="query-save-box")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

class OpenSourceScreen(ModalScreen[str | None]):
    """Choose how to read a tariff's source documents: online in the browser, or
        the local PDFs from disk. Returns 'online' | 'disk' | None.
        Only shown when BOTH source types are available; single-source cases are
        handled directly by action_open_source without pushing this screen."""

    BINDINGS = [
        Binding("1", "pick('online')", "Online"),
        Binding("2", "pick('disk')", "Disk"),
        Binding("escape", "cancel", "Cancel"),
        Binding("q", "cancel", "Cancel"),
    ]

    def __init__(self, label: str, docs: list[dict], n_urls: int,
                 n_pdfs: int, stem: str) -> None:
        super().__init__()
        self._label = label
        self._docs = docs
        self._n_urls = n_urls
        self._n_pdfs = n_pdfs
        self._stem = stem

    def compose(self) -> ComposeResult:
        i, _, t = (_esc(p) for p in self._stem.partition("__"))
        lines = [f"[bold]Quelle öffnen — {_esc(self._label)}[/bold]", ""]
        if self._docs:
            lines.append("[underline]Dokumente[/underline]")
            for dd in self._docs:
                lbl = _DOCTYPE_SHORT.get(dd.get("doctype", ""), dd.get("doctype", ""))
                fname = _esc((dd.get("file") or "")[:50])
                doc_url = dd.get("url") or ""
                if doc_url:
                    lines.append(
                        f'  [cyan]{lbl:<6}[/cyan] [link="{_esc(doc_url)}"]{fname}[/link]'
                    )
                else:
                    lines.append(f"  [cyan]{lbl:<6}[/cyan] {fname}")
            lines.append("")
        url_label = f"{self._n_urls} URL{'s' if self._n_urls != 1 else ''}"
        online = (
            f"[bold]\\[1][/bold] Alle {url_label} im Browser öffnen"
            if self._n_urls else "[dim]\\[1] Online — keine URLs hinterlegt[/dim]"
        )
        disk = (
            f"[bold]\\[2][/bold] Lokale PDFs ({self._n_pdfs}) — "
            f"[cyan]data/raw/{i}/{t}/[/cyan]"
            if self._n_pdfs else "[dim]\\[2] Lokale PDFs — keine vorhanden (\\[g] lädt)[/dim]"
        )
        lines += [online, disk, "", "[bold]\\[Esc][/bold] Abbrechen"]
        yield Container(Static("\n".join(lines)), id="open-box")

    def on_click(self, event) -> None:
        import shutil
        import subprocess
        style = getattr(event, "style", None)
        link = style.link if (style is not None and hasattr(style, "link")) else None
        if link and link.startswith("http"):
            opener = "open" if sys.platform == "darwin" else (shutil.which("xdg-open") or "xdg-open")
            subprocess.Popen([opener, link], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            event.stop()

    def action_pick(self, choice: str) -> None:
        if choice == "online" and not self._n_urls:
            return
        if choice == "disk" and not self._n_pdfs:
            return
        self.dismiss(choice)

    def action_cancel(self) -> None:
        self.dismiss(None)

class CompareTextScreen(ModalScreen[None]):
    """Full, untruncated Leistungen/Ausschlüsse across all compared tariffs, one
        category at a time. The Vergleich matrix stays compact (alignment); this is
        where the verbatim wording is read side by side — including tariffs that did
        not fit the matrix width. ↑↓ picks a category, the detail pane wraps the full
        text. Returns None (read-only)."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close"),
    ]

    def __init__(self, entries: list[dict], n_cols: int) -> None:
        super().__init__()
        self._entries = entries
        self._n_cols = n_cols

    def compose(self) -> ComposeResult:
        with Container(id="fulltext-box"):
            yield Static(
                "[bold]Volltext-Vergleich[/bold]   "
                f"[dim]{len(self._entries)} Kategorien · {self._n_cols} Tarife · "
                "↑↓ wählen · \\[Esc] schließen[/dim]",
                id="fulltext-head",
            )
            with Horizontal(id="fulltext-body"):
                yield OptionList(
                    *(
                        Option(
                            f"[{e['color']}]{e['glyph']}[/{e['color']}] {_esc(e['label'])}"
                        )
                        for e in self._entries
                    ),
                    id="fulltext-list",
                )
                yield ScrollableContainer(
                    Static("", id="fulltext-detail"), id="fulltext-detail-wrap"
                )

    def on_mount(self) -> None:
        # First option auto-highlights; render its detail right away.
        if self._entries:
            self._render_detail(0)

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        self._render_detail(event.option_index)

    def _render_detail(self, idx: int) -> None:
        if not (0 <= idx < len(self._entries)):
            return
        e = self._entries[idx]
        lines = [
            f"[bold {e['color']}]{e['glyph']} {_esc(e['label'])}[/bold {e['color']}]"
            f"   [dim]({e['section']})[/dim]",
            "",
        ]
        for label, verbatim in e["rows"]:
            if verbatim:
                lines.append(f"[bold]{_esc(label)}[/bold]")
                lines.append(f"  {_esc(verbatim)}")
            else:
                lines.append(f"[dim]{_esc(label)}: — nicht genannt[/dim]")
            lines.append("")
        try:
            self.query_one("#fulltext-detail", Static).update("\n".join(lines))
            self.query_one("#fulltext-detail-wrap").scroll_home(animate=False)
        except NoMatches:
            pass

    def action_close(self) -> None:
        self.dismiss(None)

class CompareManagerScreen(ModalScreen[list[str] | None]):
    """Manage which analyzed tariffs the Vergleich shows. The single source of
        truth is compare_stems (an include-set) in config/favorites.json — this
        modal is the one place to bulk-edit it: toggle a tariff in/out, include all,
        or clear the whole comparison. Returns the new include-set to persist on
        save, or None on cancel."""

    BINDINGS = [
        Binding("space", "toggle", "Ein/Aus"),
        Binding("enter", "toggle", "Ein/Aus"),
        Binding("a", "include_all", "Alle"),
        Binding("l", "clear_all", "Leeren"),
        Binding("s", "save", "Speichern"),
        Binding("escape", "cancel", "Abbrechen"),
        Binding("q", "cancel", "Abbrechen"),
    ]

    def __init__(self, stems: list[tuple[str, str]], included: set[str],
                 ref_stem: str | None) -> None:
        # stems: (stem, label) already in display order (reference first).
        super().__init__()
        self._stems = stems
        self._included = set(included)
        self._ref = ref_stem

    def compose(self) -> ComposeResult:
        with Container(id="compare-mgr-box"):
            yield Static(
                "[bold]Vergleich verwalten[/bold]   "
                "[dim]↑↓ wählen · Space/Enter ein-/ausblenden · "
                "\\[a] alle einblenden · \\[l] leeren · \\[s] speichern · "
                "\\[Esc] abbrechen[/dim]",
                id="compare-mgr-head",
            )
            yield OptionList(id="compare-mgr-list")

    def on_mount(self) -> None:
        self._refresh_list()

    def _row_text(self, stem: str, label: str) -> str:
        shown = stem in self._included
        box = "[green]\\[x][/green]" if shown else "[dim]\\[ ][/dim]"
        ref = " [yellow](Ref)[/yellow]" if stem == self._ref else ""
        name = label if shown else f"[dim]{label}[/dim]"
        return f"{box} {name}{ref}"

    def _refresh_list(self) -> None:
        try:
            lst = self.query_one("#compare-mgr-list", OptionList)
        except NoMatches:
            return
        keep = lst.highlighted
        lst.clear_options()
        for stem, label in self._stems:
            lst.add_option(Option(self._row_text(stem, label), id=stem))
        n_shown = sum(1 for s, _ in self._stems if s in self._included)
        self.query_one("#compare-mgr-head", Static).update(
            "[bold]Vergleich verwalten[/bold]   "
            f"[dim]{n_shown}/{len(self._stems)} im Vergleich · ↑↓ wählen · "
            "Space/Enter ein-/ausblenden · \\[a] alle · \\[l] leeren · "
            "\\[s] speichern · \\[Esc] abbrechen[/dim]"
        )
        if self._stems and keep is not None:
            lst.highlighted = min(keep, len(self._stems) - 1)

    def _highlighted_stem(self) -> str | None:
        lst = self.query_one("#compare-mgr-list", OptionList)
        if lst.highlighted is None:
            return None
        return self._stems[lst.highlighted][0]

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        # Enter / click — the OptionList consumes enter before the screen
        # binding, so toggle from here too.
        stem = event.option.id
        if stem is not None:
            self._included.symmetric_difference_update({stem})
            self._refresh_list()

    def action_toggle(self) -> None:
        stem = self._highlighted_stem()
        if stem is None:
            return
        self._included.symmetric_difference_update({stem})
        self._refresh_list()

    def action_include_all(self) -> None:
        self._included = {stem for stem, _ in self._stems}
        self._refresh_list()

    def action_clear_all(self) -> None:
        # Include none — the matrix then shows its "leer" empty state, which tells
        # the user how to add tariffs back (Market [a] / toggle here).
        self._included.clear()
        self._refresh_list()

    def action_save(self) -> None:
        self.dismiss(sorted(self._included))

    def action_cancel(self) -> None:
        self.dismiss(None)


class NeedsEditorScreen(ModalScreen["dict[str, float] | None"]):
    """Edit the personal Bedarf weighting (config/needs-weights.json) in the TUI — one
        relevance level per Baustein that re-weights the Magic-Find module_breadth
        dimension. Discrete 0–3 scale (egal/normal/wichtig/kritisch); finer floats can
        still be hand-edited in the JSON. Returns the new weights on save, None on
        cancel."""

    LEVELS = {0: "egal", 1: "normal", 2: "wichtig", 3: "kritisch"}
    MAX_LEVEL = 3

    BINDINGS = [
        Binding("right", "bump(1)", "+"),
        Binding("plus", "bump(1)", "+"),
        Binding("equals_sign", "bump(1)", "+"),
        Binding("left", "bump(-1)", "−"),
        Binding("minus", "bump(-1)", "−"),
        Binding("0", "set_level(0)", "0"),
        Binding("1", "set_level(1)", "1"),
        Binding("2", "set_level(2)", "2"),
        Binding("3", "set_level(3)", "3"),
        Binding("r", "reset", "Neutral"),
        Binding("s", "save", "Speichern"),
        Binding("escape", "cancel", "Abbrechen"),
        Binding("q", "cancel", "Abbrechen"),
    ]

    def __init__(self, keys_labels: list[tuple[str, str]],
                 current: dict[str, float]) -> None:
        # keys_labels: (module_key, German label) in display order.
        super().__init__()
        self._kl = list(keys_labels)
        self._w = {k: float(current.get(k, 1.0)) for k, _ in self._kl}

    def compose(self) -> ComposeResult:
        with Container(id="needs-box"):
            yield Static("", id="needs-head")
            yield OptionList(id="needs-list")

    def on_mount(self) -> None:
        self._refresh()

    def _level(self, key: str) -> int:
        return max(0, min(self.MAX_LEVEL, int(round(self._w.get(key, 1.0)))))

    def _row_text(self, key: str, label: str) -> str:
        lvl = self._level(key)
        bar = ("[green]" + "●" * lvl + "[/green]"
               + "[dim]" + "○" * (self.MAX_LEVEL - lvl) + "[/dim]")
        padded = f"{label:<22}"
        if lvl == 0:
            padded = f"[dim]{padded}[/dim]"
        return f"{padded} {bar}  [dim]{self.LEVELS[lvl]}[/dim]"

    def _refresh(self) -> None:
        try:
            lst = self.query_one("#needs-list", OptionList)
        except NoMatches:
            return
        keep = lst.highlighted
        lst.clear_options()
        for key, label in self._kl:
            lst.add_option(Option(self._row_text(key, label), id=key))
        neutral = len({self._level(k) for k, _ in self._kl}) == 1
        self.query_one("#needs-head", Static).update(
            "[bold]🎯 Bedarf-Gewichte[/bold]   "
            + ("[dim](neutral — alle gleich)[/dim]" if neutral
               else "[yellow](aktiv)[/yellow]")
            + "\n[dim]↑↓ Baustein · ←/− /→/+ ändern · 0–3 direkt setzen · "
            "\\[r] neutral · \\[s] speichern · \\[Esc] abbrechen[/dim]"
        )
        if self._kl:
            # default to the first row so +/-/digit keys act immediately (no need to
            # arrow-down first), and keep the cursor across refreshes.
            lst.highlighted = (min(keep, len(self._kl) - 1)
                               if keep is not None else 0)

    def _highlighted_key(self) -> str | None:
        lst = self.query_one("#needs-list", OptionList)
        if lst.highlighted is None:
            return None
        return self._kl[lst.highlighted][0]

    def action_bump(self, delta: int) -> None:
        key = self._highlighted_key()
        if key is None:
            return
        self._w[key] = float(max(0, min(self.MAX_LEVEL, self._level(key) + delta)))
        self._refresh()

    def action_set_level(self, level: int) -> None:
        key = self._highlighted_key()
        if key is None:
            return
        self._w[key] = float(max(0, min(self.MAX_LEVEL, level)))
        self._refresh()

    def action_reset(self) -> None:
        self._w = {k: 1.0 for k, _ in self._kl}
        self._refresh()

    def action_save(self) -> None:
        self.dismiss(dict(self._w))

    def action_cancel(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    """Full keyboard reference, grouped. The footer shows only the essentials."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("question_mark", "close", "Close"),
        Binding("q", "close", "Close"),
        Binding("enter", "close", "Close"),
    ]

    GROUPS = [
        ("Navigation", [
            ("y / x / v / l / B", "Favoriten / Markt / Vergleich / Verlauf / Benchmark"),
            ("M", "Magic Find — markt-weites Qualitäts-Ranking (Preis zählt nicht)"),
            ("P", "Bedarf-Modus an/aus — Module nach deiner Gewichtung (needs-weights.json)"),
            ("W", "Bedarf-Gewichte bearbeiten — Relevanz je Baustein (0–3) setzen"),
            ("F", "Markt-Scan — Top-Kandidaten live harvesten + analysieren, dann ranken"),
            ("Tab / ⇧Tab", "nächster / voriger Tab (zyklisch)"),
            ("↑ ↓ / Klick", "Zeile wählen (aktualisiert das Detail-Band)"),
            ("d", "Detail-Band unter der Tabelle ein/aus"),
        ]),
        ("Tarif-Aktionen (markierte Zeile)", [
            ("g", "Quell-PDFs laden + analysieren"),
            ("G", "nur analysieren (PDFs lokal, kein Download)"),
            ("H", "live harvesten (Browser) + laden + analysieren — wenn keine URLs da"),
            ("a", "zum Vergleich hinzufügen/entfernen (analysiert bei Bedarf)"),
            ("o", "Quelle öffnen (online im Browser / lokale PDFs)"),
            ("R", "als Referenz setzen — Δ rechnet neu"),
            ("u", "Favorit an/aus"),
            ("N", "Notiz zum Favoriten bearbeiten"),
            ("D", "lokale Daten löschen (Umfang im Dialog)"),
        ]),
        ("Vergleich \\[v]", [
            ("a", "markierten Tarif zum Vergleich hinzufügen/entfernen"),
            ("c", "Vergleich verwalten — Tarife ein-/ausschalten, leeren, alle"),
            ("w", "Wortlaut ein/aus (kompakt ↔ ausführlich)"),
            ("t", "Volltext-Modal: ganze Texte je Kategorie, alle Tarife"),
        ]),
        ("Markt", [
            ("f", "Filter (Versicherer / Produkt)"),
            ("Esc", "Filter leeren"),
            ("s / n / p / j", "Sortierung: €/Monat · Note · Position · Jüngste Änderung"),
        ]),
        ("Werkzeuge", [
            ("b", "CHECK24-Query-URL bauen (nur Ansicht)"),
            ("e", "CHECK24-Suche bearbeiten (Levers ändern + speichern)"),
            ("r", "Daten neu laden"),
            ("T", "Theme wechseln (rose-pine, nord, dracula, …)"),
            ("?", "diese Hilfe"),
            ("q", "Beenden"),
        ]),
    ]

    def compose(self) -> ComposeResult:
        lines = ["[bold]check0r3000 — Shortcuts[/bold]", ""]
        for title, items in self.GROUPS:
            lines.append(f"[underline]{title}[/underline]")
            for key, desc in items:
                lines.append(f"  [bold cyan]{key:<13}[/bold cyan] {desc}")
            lines.append("")
        lines.append("[bold]\\[Esc][/bold] Schließen")
        yield Container(Static("\n".join(lines)), id="help-box")

    def action_close(self) -> None:
        self.dismiss(None)

class NoteEditScreen(ModalScreen[str | None]):
    """Edit the free-text note on a favorite. A multi-line TextArea prefilled with
        the current note; Ctrl+S saves, Esc cancels. Returns the new note text
        (possibly empty, to clear the note) on save, or None on cancel."""

    BINDINGS = [
        Binding("ctrl+s", "save", "Speichern"),
        Binding("escape", "cancel", "Abbrechen"),
    ]

    def __init__(self, title: str, note: str) -> None:
        super().__init__()
        self._title = title
        self._note = note

    def compose(self) -> ComposeResult:
        with Container(id="note-box"):
            yield Static(
                f"[bold]Notiz — {_esc(self._title)}[/bold]\n"
                "[dim]Gedanken/Kontext zu diesem Favoriten · "
                "\\[Strg+S] speichern · \\[Esc] abbrechen[/dim]",
                id="note-head",
            )
            yield TextArea(self._note, id="note-input", soft_wrap=True)

    def on_mount(self) -> None:
        self.query_one("#note-input", TextArea).focus()

    def action_save(self) -> None:
        self.dismiss(self.query_one("#note-input", TextArea).text)

    def action_cancel(self) -> None:
        self.dismiss(None)
