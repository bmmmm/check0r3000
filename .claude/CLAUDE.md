# check0r3000 — CLAUDE.md

Rechtsschutzversicherungs-Vergleichstool. Scraped CHECK24, extrahiert Leistungsfakten per
LLM aus Versicherer-PDFs, und zeigt alles in einer lokalen Textual-TUI. Kein DB-Engine,
nur Dateien + stdlib + uv.

## Dateistruktur (was wohin gehört)

```
scripts/          Alle ausführbaren Scripts (uv-Shebang, direkt startbar)
config/           Query-Profil + Konfiguration (check24-profile.json gitignored: PII)
data/             Lokale Rohdaten — alles gitignored außer sources/
  inbox/          Sammelordner für neue PDFs
  raw/<stem>/     Klassifizierte PDFs (intake.py sortiert ein)
  extracted/      Klartext aus PDFs (ingest.py, gitignored)
  snapshots/      Datierte CHECK24-Preislisten (gitignored)
  sources/        check24-documents.json — getrackte URL-Manifest (kein PDF-Inhalt)
  offers/         Persönliche Beitrags-/Stufendaten (gitignored; nur _example + README getrackt)
out/              Ergebnisse — getrackt außer enriched/ und screenshots/
  tariffs/        Reine LLM-Fakten-Records (beitrag immer null)
  enriched/       Mit Beitrag/Stufe angereichert (gitignored, persönlich)
  tariff-history/ Versionierte Records je Stem (content-hash-basiert, getrackt)
  vergleich.md    Synthesierter Vergleich (getrackt)
  index.html      HTML-Version
benchmarks/       golden.json + Regression-Digest (getrackt)
schema/           JSON-Schemas (tariff + offer)
```

## Der `stem` ist die einzige ID

Jeder Tarif hat einen kanonischen `stem` = `<versicherer>__<tarif>` (aus
`data/sources/check24-documents.json`). TUI, Pipeline-Output, Doc-Manifest, Tariff-History
und alle Scripts hängen daran. Nie ad-hoc Pfade konstruieren — immer via stem-Lookup.

## Pipeline-Invarianten

- `out/tariffs/<stem>.json` enthält **nie** Beitrag, Stufe oder SB — die kommen nur via
  `overlay.py` aus `data/offers/` in `out/enriched/`. `regression.py` pinnt das.
- `extract.py` ruft nach dem Schreiben automatisch `feature_history.archive_version()` auf.
- `PROMPT_VERSION` in `extract.py` erhöhen wenn das Schema sich ändert (invalidiert Cache).
- `pipeline.sh` läuft immer `regression.py` am Ende (nicht-fatal, aber laut).

## TUI-Architektur (4 Module)

```
tui.py           ~270-Zeilen Entry-Point + uv-Shebang
tui_data.py      Textual-freie Daten-/Lade-Schicht; python3 tui_data.py --selftest
tui_format.py    Rendering-Helpers (Rich-Markup, Normalisierung)
tui_screens.py   Widget- und Screen-Definitionen
tui_app.py       App-Klasse, Bindings, alle Actions
```

Kreisimport-Falle: `tui_screens.py` importiert `tui_data/tui_format`, nie `tui_app`.
`tui_app.py` importiert alles. `tui.py` importiert nur `tui_app`.

## Key shortcuts (TUI)

| Key | Aktion |
|---|---|
| `[g]` | Fetch + Analyse (fetch_docs → ingest → extract) |
| `[G]` | Nur Analyse wenn PDFs schon lokal |
| `[H]` | Live-Harvest via Playwright + Analyse (für Tarife ohne Manifest) |
| `[a]` | Tarif in Vergleich aufnehmen / entfernen (Market-Tab) |
| `[u]` | Favorit an/aus |
| `[l]` | Verlauf-Tab (Snapshot-Diff + Feature-Diff) |
| `[x]` | Markt-Tab (Tarifliste) |
| `[v]` | Vergleich-Tab (Coverage-Matrix) |
| `[B]` | Benchmark-Tab (Modell-Scorecard aus `benchmarks/results.json`) |
| `[d]` | Detail-Band ein/aus |
| `Tab` / `⇧Tab` | Nächster / voriger Tab (zyklisch) |
| `[?]` | Alle Shortcuts |

## Wichtige Constraints

- **Keine PDFs committen** — `data/raw/`, `data/extracted/`, `data/inbox/` gitignored.
  Nur `data/sources/check24-documents.json` (URLs, kein Inhalt) ist getrackt.
- **Kein PII in getrackte Files** — `config/check24-profile.json` gitignored.
  `config/favorites.json` enthält nur stem/tag/SB-Band, keine Preise.
- **Modell-Spec** immer als `[provider:]model[@endpoint]` (via `_providers.py`).
  API-Keys aus Umgebung (`OMLX_API_KEY`, `OPENAI_API_KEY`), nie im Code.
- **`CHECK0R_ANALYZE_MODEL`** steuert das Modell für TUI-gestartete Analysen.

## Textual-Gotchas

- `[link={url}]` mit einer URL im Wert crasht auf mount → immer `[link="{url}"]` (quoted).
- SVG-Screenshots via `--screenshot` sind non-deterministisch (Font-Metrik-Abhängigkeit) —
  nie per `diff` vergleichen; stattdessen `ast.dump` oder inhaltliche Assertions.
- Textual 8.x: `[link=value]` muss gequotet sein wenn value ein URL-Sonderzeichen enthält.

## Modell-Empfehlung

`haiku --filter` ist der Sweet-Spot: getrimmte AVB passt ins 200k-Fenster, ~7× günstiger
als Opus, Korrektheit vergleichbar. `--repeat 3` bei günstigeren Modellen wegen
Run-to-Run-Varianz. Lokale Backends (`omlx:`, `mlx:`, `ollama:`) haben `cost_usd = null`.
