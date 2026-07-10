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

## Shared Leaf-Module (stdlib-only, nie neu erfinden)

- `scripts/_modules.py` — einzige Quelle für Baustein-Keys/-Labels (aus dem Schema).
- `scripts/_jsonio.py` — `atomic_write_json` (tmp+`os.replace`) + `load_json_or`; jeder
  neue JSON-Writer/-Loader nutzt die. (Heißt `_jsonio`, weil `_io.py` das CPython-Builtin
  schatten würde.)
- `scripts/_manifest.py` — einziger Loader für `data/sources/check24-documents.json`
  (fetch_docs/harvest_docs/intake); `create_if_missing=True` nur für harvest-Erstlauf.

## Der `stem` ist die einzige ID

Jeder Tarif hat einen kanonischen `stem` = `<versicherer>__<tarif>` (aus
`data/sources/check24-documents.json`). TUI, Pipeline-Output, Doc-Manifest, Tariff-History
und alle Scripts hängen daran. Nie ad-hoc Pfade konstruieren — immer via stem-Lookup.

## Pipeline-Invarianten

- **`update-all.sh` = Voll-Refresh:** Scan+Snapshot (`fetch_ratings --snapshot`) → Docs
  (`fetch_docs --apply --into-raw`) → `pipeline.sh`. Nur die Scan-Phase braucht echten
  Chromium (Sandbox-Bypass oder `--no-scan`); `rechtsschutz.check24.de` ist in
  `settings.local.json` sandbox-allowlisted → fetch_docs/Downloads laufen in-sandbox.
  `--jobs`/`--repeat` werden bis zu `extract.py` durchgereicht — **immer die Record-
  Provenance matchen** (aktuell `--model haiku --filter --repeat 3`), sonst re-extrahiert
  der Cache-Signatur-Mismatch still alle Tarife kostenpflichtig.
- **Verlauf-Statistik:** `price_history.market_stats()` (CLI: `--market`) aggregiert je
  Snapshot count/min/median/max; TUI-Verlauf zeigt „Markt über Zeit"-Headerzeile +
  Preisverlauf-Sparkline je Stem (`ChangeInfo.price_series`, gepinnte SB-Variante).
- `out/tariffs/<stem>.json` enthält **nie** Beitrag, Stufe oder SB — die kommen nur via
  `overlay.py` aus `data/offers/` in `out/enriched/`. `regression.py` pinnt das.
- `extract.py` ruft nach dem Schreiben automatisch `feature_history.archive_version()` auf.
- `PROMPT_VERSION` in `extract.py` erhöhen wenn das Schema sich ändert (invalidiert Cache).
- `pipeline.sh` läuft immer `regression.py` am Ende (nicht-fatal, aber laut).
- **Magic Find scored Beitrag/Preis NIE** — `magic.py` ist read-only über `out/tariffs/`;
  Preis ist nur Anzeige + letzter Tiebreaker bei Score-Gleichstand. Gewichte + `pool_k`
  aus `config/magic-weights.json` (getrackt, kein PII; jede Teilmenge übersteuert die
  Code-Defaults in `MagicWeights`). `leistung_cov` zählt distinkte `coverage_taxonomy`-
  Kategorien, nicht rohe leistungen — Tarife mit Benefits außerhalb der Taxonomie werden
  dadurch untergewichtet (bekannte Grenze, kein Extraction-Bug).
- **5 gewertete Dimensionen** (Default-Gewichte, Summe 1.0): `note` 0.35, `leistung_cov`
  0.22, `module_breadth` 0.20, `coverage_gen` 0.18, `bewertung` 0.05. `module_tier` ist
  **nicht mehr gewertet** (nur ~3/26 Records tragen ein Level — gepinntes `level:null`
  gegen haiku-Halluzination; eine Tier-Gewichtung belohnte Extraktions-Vollständigkeit,
  nicht Qualität) → bleibt als `MagicScore.module_tier_raw` reine Detail-Anzeige.
- **Bedarf-Toggle `[P]`** — `config/needs-weights.json` (getrackt, kein PII, neutral-1.0-
  Placeholder) gewichtet **nur** `module_breadth` nach persönlichem Bedarf. Neutrale
  Gewichte = identisch zum objektiven Ranking. `needs=None` (Default) = objektiv.
  **`[W]`** öffnet den `NeedsEditorScreen` (diskrete 0–3-Skala je Baustein,
  `magic.save_needs` schreibt die JSON, behält `_comment`); ein non-neutraler Save
  schaltet `[P]` automatisch an. Feinere Floats bleiben per Hand-Edit der JSON möglich.
- **Konfidenz-Flag** — `leistung_low_confidence` (in `rank()` gesetzt) markiert Records,
  deren distinkte-Leistungs-Zahl weit unter dem Markt-Median liegt (Recall-Lücke, kein
  armer Tarif); rein Anzeige (⚠), der Score bleibt unangetastet. `quality_per_eur()` =
  reine Preis-Leistungs-Anzeige (Spalte „P/L"), nie ein Score-Input.

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

Per-Tab-Dispatch läuft über die `TAB_SPECS`-Tabelle in `tui_app.py` (TabSpec je Tab:
Table-/Band-/Focus-IDs + Adopt-Handler) — ein neuer Tab ist EIN TabSpec-Eintrag, keine
verstreuten if-Ketten. `_adopt_cursor_row` bleibt der einzige Writer des Active-State.
URLs in `[link="…"]` immer durch `tui_format.link_url()` (percent-encoded `"`/`[`/`]`).

## Key shortcuts (TUI)

| Key | Aktion |
|---|---|
| `[g]` | Fetch + Analyse (fetch_docs → ingest → extract) |
| `[G]` | Nur Analyse wenn PDFs schon lokal |
| `[H]` | Live-Harvest via Playwright + Analyse (für Tarife ohne Manifest) |
| `[F]` | Markt-Scan (Deep-Scan): Top-K vorab-bewertete fehlende Tarife harvesten+analysieren, dann neu ranken |
| `[U]` | Update-All: Scan+Snapshot → Docs → volle Re-Analyse (Extract-Flags aus Record-Provenance, TUI-Zwilling von `update-all.sh`) |
| `[a]` | Tarif in Vergleich aufnehmen / entfernen (Market-Tab) |
| `[u]` | Favorit an/aus |
| `[l]` | Verlauf-Tab (Snapshot-Diff + Feature-Diff) |
| `[x]` | Markt-Tab (Tarifliste) |
| `[v]` | Vergleich-Tab (Coverage-Matrix) |
| `[B]` | Benchmark-Tab (Modell-Scorecard aus `benchmarks/results.json`) |
| `[M]` | Magic-Find-Tab (markt-weites Qualitäts-Ranking; Preis fließt NIE in den Score) |
| `[P]` | Bedarf-Modus an/aus (Module nach `config/needs-weights.json` gewichten) |
| `[W]` | Bedarf-Gewichte bearbeiten (Editor: Relevanz je Baustein 0–3) |
| `[d]` | Detail-Band ein/aus |
| `Tab` / `⇧Tab` | Nächster / voriger Tab (zyklisch) |
| `[?]` | Alle Shortcuts |

## Wichtige Constraints

- **Keine PDFs committen** — `data/raw/`, `data/extracted/`, `data/inbox/` gitignored.
  Nur `data/sources/check24-documents.json` (URLs, kein Inhalt) ist getrackt.
- **Kein PII in getrackte Files** — `config/check24-profile.json` gitignored.
  `config/favorites.json` enthält nur stem/tag/SB-Band, keine Preise. `[N]`-Notizen
  landen im gitignorten Sidecar `config/favorite-notes.json` (stem-keyed), nie in
  favorites.json.
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
als Opus, Korrektheit vergleichbar. `--repeat N` (z.B. 3) extrahiert jeden Tarif N-mal und
**unioniert `leistungen`/`ausschluesse`** über die Runs (alle anderen Felder vom
vollständigsten Run) — dämpft die Omissions-Varianz billiger Modelle (Recall ~2–3×).
`--only <stem>…` beschränkt auf eine Shortlist. `--jobs N` (Default 1) parallelisiert
über Tarife — 2–4 ist wegen Provider-Rate-Limits die sinnvolle Spanne. **Vorsicht bei kuratierten Tarifen:**
Re-Extraktion überschreibt Hand-Patches (z.B. arags qualitative SB, golden-gepinnte
`module.level: null` — haiku halluziniert sonst `level` aus dem Tarifnamen); nach
`--repeat` auf gepinnte/kuratierte Stems immer gegen HEAD reconcilen + `regression.py`.
Lokale Backends (`omlx:`, `mlx:`, `ollama:`) haben `cost_usd = null`.
