# check0r3000

```
  ▄████ ██  ██ ██████  ▄████ ██  ██  ████  █████  █████   ████   ████   ████
 ██     ██  ██ ██     ██     ██ ██  ██  ██ ██  ██     ██ ██  ██ ██  ██ ██  ██
 ██     ██████ █████  ██     ████   ██  ██ █████    ███  ██  ██ ██  ██ ██  ██
 ██     ██  ██ ██     ██     ██ ██  ██  ██ ██ ██      ██ ██  ██ ██  ██ ██  ██
  ▀████ ██  ██ ██████  ▀████ ██  ██  ████  ██  ██ █████   ████   ████   ████

                          » VERSICHERUNGS-VERGLEICH «
```

**Versicherungsbedingungen rein, vergleichbare Fakten raus — für ~20 Cent pro
Tarif.** check0r3000 liest die Original-AVB der Versicherer, extrahiert per LLM
schema-valide, regressionsgetestete Leistungsdaten und rankt ganze
Versicherungs-Märkte nach Qualität — mit Preisverlauf, Feature-Diffs
über die Zeit („was hat der Versicherer still gestrichen?") und externen
Testurteilen. Multi-Vertical: **Rechtsschutz**, **Hausrat** und
**Privathaftpflicht** sind alle drei `production` und gleichberechtigt — die TUI
öffnet mit der Sparten-Auswahl, `[S]` wechselt sie zur Laufzeit. Kein DB-Engine,
keine schweren Frameworks:
**Dateien + Python-stdlib + `uv`**, Modell-Backend frei wählbar (Claude-Cloud
oder lokal via Ollama/oMLX/mlx).

`Python + uv` · `Textual-TUI` · `modellfrei wo immer möglich` · `GPL-3.0`

> **In English:** German insurance policy terms (AVB) are unstructured legal
> prose, formatted differently by every insurer. check0r3000 uses an LLM for
> exactly one step — normalising that prose into a fixed schema — and keeps the
> rest deterministic. The extraction is measured rather than assumed: golden
> files per line of business, scored on faithfulness, schema validity,
> hallucination-freedom and module coverage, with a regression run after every
> pipeline pass. The headline finding is that **what you feed the model matters
> more than which model** — on the same contract with the same input, a local
> 20B model scored 100 where Opus scored 57.
> See the [scorecard](benchmarks/rechtsschutz/scorecard.md) and
> [how the benchmark works](#-benchmark-modell---input-vergleich).

> **Datenherkunft & Spielregeln:** Die Fakten kommen aus den Bedingungswerken
> (AVB/PIB) — öffentlich zugängliche Dokumente der Versicherer. Die PDFs selbst
> bleiben lokal (fremdes Urheberrecht); getrackt sind nur abgeleitete Fakten und
> URL-Manifeste. Die Markt-Sicht (Preise, Tarifnoten, Verlauf) entsteht aus
> einem persönlichen, rate-limited CHECK24-Scan für den Eigenbedarf — gescrapte
> Rohdaten werden weder committet noch weiterverbreitet.
> Details: [Was getrackt wird](#-was-getrackt-wird-und-was-nicht).

---

## ✨ Highlights

- 🧭 **Multi-Vertical** — jede Sparte lebt in einem eigenen Namespace
  (`data/<sparte>/`, `out/<sparte>/`, `schema/<sparte>/`,
  `config/verticals/<sparte>/`), Registry in `config/verticals.json`;
  Sparten-Spezifika (Module, Filter-Anker, Extraktions-Prompt, Harvest-Flow)
  sind **Daten**, nicht Code. `[S]` wechselt zur Laufzeit;
  `scripts/new_vertical.py` scaffoldet eine neue Sparte aus Probe-Evidenz.
- 🎬 **Boot-Splash mit Stil** — drei prozedurale Logo-Animationen (Implosion,
  Big Bang, Slam), zufällig pro Start. `CHECK0R_SPLASH=1|2|3|random|off`.
  Standalone-Demo: `python3 scripts/tui_anim.py 2`.
- ✨ **Magic Find** — markt-weites Qualitäts-Ranking über alle analysierten
  Tarife. **Der Preis fließt nie in den Score** (nur Anzeige + P/L-Spalte);
  auf Wunsch gewichtet nach persönlichem Bedarf (`[P]`/`[W]`).
- 🏆 **Externe Testurteile** — Finanztip-Empfehlungen, Franke & Bornberg FFF+
  und Finanztest-Sieger als kuratierte, display-only Badges direkt am Tarif —
  inklusive des blinden Flecks: extern empfohlene Direktversicherer, die auf
  CHECK24 gar nicht auftauchen.
- 📈 **Verlauf-Datenbank** — datierte Markt-Snapshots mit „Markt über Zeit"-
  Statistik, Preisverlauf-Sparklines je Tarif (▁▂▄▆█) und Leistungs-Diffs
  („was hat der Versicherer still gestrichen?").
- 🔄 **Ein-Kommando-Refresh** — `./update-all.sh` bzw. `[U]` in der TUI:
  Scan → Snapshot → Doc-Downloads → Re-Analyse. Die Extract-Flags werden aus
  der Record-Provenance abgeleitet, damit unveränderte Tarife **nichts kosten**.
- 🧪 **Eingebauter Modell-Benchmark** — Cloud vs. lokal auf denselben Dokumenten,
  mit Halluzinations-Guard, Kosten- und Varianz-Messung (`[B]`-Tab).
- 🛡️ **Regression über alles** — golden-Invarianten + marktweiter Schema-Check
  nach jedem Pipeline-Lauf; ein Modellwechsel, der Fakten fallen lässt, fällt auf.

## 🚀 Quick Start

Voraussetzungen: [`uv`](https://docs.astral.sh/uv/) auf dem PATH; für die
Analyse-Stufen die `claude`-CLI **oder** ein lokales OpenAI-kompatibles Backend.

```sh
uv run scripts/tui.py            # die TUI — alles Weitere geht von hier
./update-all.sh                  # Voll-Refresh: Scan → Docs → Analyse
./pipeline.sh --model haiku --filter   # nur die Analyse-Pipeline
```

Die TUI von überall starten (uv-Shebang + pfad-auflösender Entry-Point):

```sh
ln -s "$(pwd)/scripts/tui.py" ~/.local/bin/check0r3000   # einmalig
check0r3000                                              # fragt, welche Sparte
check0r3000 --vertical hausrat                           # direkt hinein
```

## 🖥️ Die TUI — sechs Tabs

`scripts/tui.py` ist das einzige Skript mit `textual`-Dependency; alles andere
ist stdlib. Beim Start wird zuerst die Sparte gewählt (übersprungen mit
`--vertical` / `CHECK0R_VERTICAL`), dann läuft der Boot-Splash; während
Pipelines laufen ein zentrierter Loader mit Statuszeile.

| Tab | Taste | Was |
|---|---|---|
| ★ **Favorites** | `[y]` | Kuratierte Shortlist: Note/Preis/SB, Δ vs. Referenz, SB-Varianten, Dokument-URLs, Notizen (`[N]`) |
| **Market** | `[x]` | Alle Tarife des Snapshots, sortier-/filterbar, Status je Zeile (`✓ analysiert · ↓ PDF lokal · ○ URLs`) |
| **Vergleich** | `[v]` | Coverage-Matrix nebeneinander: Module, Deckung, Leistungen/Ausschlüsse taxonomie-normalisiert |
| **Verlauf** | `[l]` | Snapshot-Diff + „Markt über Zeit"-Statistik + Preis-Sparkline + Leistungsänderungen je Tarif |
| **Benchmark** | `[B]` | Modell-Scorecard aus `benchmarks/<sparte>/results.json` — welches Modell extrahiert am treuesten? |
| ✨ **Magic Find** | `[M]` | Qualitäts-Ranking über den ganzen analysierten Markt, Preis zählt nicht |

Die wichtigsten Aktionen (`[?]` zeigt alle):

| Taste | Aktion |
|---|---|
| `[g]` / `[G]` | Download + Analyse / nur Analyse (PDFs schon lokal) |
| `[H]` | Quell-URLs **live ernten** (Playwright) + direkt analysieren |
| `[F]` | Markt-Scan: Top-K vielversprechende, noch nicht analysierte Tarife holen + ranken |
| `[U]` | **Update-All**: Scan+Snapshot → Docs → volle Re-Analyse (Zwilling von `update-all.sh`) |
| `[a]` / `[u]` | Tarif zum Vergleich hinzufügen/entfernen / Favorit an-aus |
| `[P]` / `[W]` | Bedarf-Modus an/aus / Bedarf-Gewichte-Editor (0–3 je Baustein) |
| `[S]` | Sparte wechseln (Registry-basiert, mit Tarif-Zahl je Sparte; erscheint auch beim Start) |
| `[d]` | Detail-Band ein/aus (voller Record, Score-Breakdown, externe Bewertungen, Preisverlauf) |
| `[R]` / `[D]` / `[o]` / `[O]` | Δ-Referenz setzen / lokale Daten löschen / Quelldokumente öffnen / Auf CHECK24 öffnen |

**Identität über den `stem`:** jeder Tarif hat eine kanonische ID
`<versicherer>__<tarif>` (aus `data/<sparte>/sources/check24-documents.json`).
TUI, Pipeline-Output, Doc-Manifest und Tarif-History hängen alle daran; Stems
sind innerhalb ihrer Sparte eindeutig.

**Architektur:** `tui.py` ist ein ~270-Zeilen-Entry-Point über fünf
sibling-Modulen — `tui_data.py` (Textual-freie Datenschicht, `--selftest`),
`tui_format.py` (Rendering), `tui_screens.py` (Widgets/Screens), `tui_app.py`
(App/Bindings/Actions), `tui_anim.py` (Splash + Loader, Textual-frei).
`scripts/tui_test.py` fährt die echte App headless durch die volle
Pilot-Testsuite (Tab-Wechsel, Cross-Tab-State, Markup-feindliche Daten, Modals).

## ✨ Magic Find — Qualität ranken, nicht den Preis

`scripts/magic.py` ist reine Arithmetik über `out/<sparte>/tariffs/` (kein
Modell-Call, read-only). Fünf gewichtete Dimensionen
(`config/verticals/<sparte>/magic-weights.json`):
CHECK24-Tarifnote, Leistungs-Abdeckung (distinkte Taxonomie-Kategorien),
Modul-Breite, Deckungs-Generosität, Kundenbewertung. **Der Beitrag ist nie ein
Score-Input** — er erscheint nur als Anzeige und in der P/L-Spalte
(Preis-Leistung, ebenfalls nur Anzeige).

- `[P]` gewichtet die Modul-Breite nach persönlichem Bedarf
  (`config/verticals/<sparte>/needs-weights.json`, neutral = identisch zum
  objektiven Ranking); `[W]` öffnet den Editor dafür.
- ⚠ markiert Tarife, deren Extraktion dünn aussieht (Recall-Lücke, kein
  schlechter Tarif) — der Score bleibt unangetastet.
- `[F]` schließt Lücken: bewertet alle **nicht** analysierten Markt-Tarife vor
  (Note/Bewertung/Preis aus dem Snapshot), harvestet die Top-K und rankt neu.

## 🏆 Externe Testurteile (display-only)

`data/<sparte>/sources/external-ratings.json` (getrackt, handkuratiert mit
Quelle + Stand) hängt Finanztip-/Franke&Bornberg-/Finanztest-Urteile als Badges an
Tarife und Versicherer: `FT ✓` Empfehlung, `FFF+` Top-Rating, `FT ✗` keine
Empfehlung. Sichtbar im Detail-Band („Externe Bewertungen") und als
**Ext**-Spalte im Magic-Find-Tab.

Bewusst **nie ein Score-Input** — externe Tests decken nur einen Bruchteil des
Markts, eine Gewichtung würde „getestet" mit „gut" verwechseln (dieselbe Regel
wie beim Preis). Der Magic-Header nennt zusätzlich die extern empfohlenen
Tarife, die es **gar nicht auf CHECK24 gibt** (Direktvertrieb — der
strukturelle blinde Fleck jedes Vergleichsportals).

Staleness-Check ohne Scraping: `scripts/check_external_ratings.py` greppt das
hinterlegte Stand-Datum auf der Quellseite und warnt, wenn es verschwunden ist.

## 🔄 Voll-Refresh: `update-all.sh`

```sh
./update-all.sh              # Scan+Snapshot → Docs → Analyse-Pipeline
./update-all.sh --no-scan    # ohne Playwright/Chromium (z. B. in Sandboxen)
```

Drei Phasen: `fetch_ratings.py --snapshot` (headless Chromium → datierter
Markt-Snapshot), `fetch_docs.py --apply --into-raw` (Manifest-PDFs laden),
`pipeline.sh` (ingest → extract → render → regression). Scan- und Docs-Phase
sind non-fatal — ein Netz-Hickser bricht den Refresh nicht ab.

**Cache-sicher by construction:** ohne explizite Flags leitet das Skript
`--model/--filter/--repeat` aus der dominanten Provenance der bestehenden
Records ab (`tui_data.py --provenance`). Die Extract-Cache-Signatur enthält
alle drei — ein abweichender Spec würde sonst still **jeden** Tarif
kostenpflichtig neu extrahieren. Unveränderte PDFs kosten so: nichts.

## 📈 Verlauf: der Markt über die Zeit

`scripts/snapshot.py` legt pro Lauf einen datierten Snapshot der ganzen
CHECK24-Ergebnisliste ab; `scripts/price_history.py` rechnet daraus je Tarif
eine an **eine** SB-Variante gepinnte Preis-Serie (kein Phantom-Diff durch
Varianten-Mischung) und marktweite Aggregate (`--market`: count/min/median/max
je Snapshot).

Der Verlauf-Tab zeigt beides: die „Markt über Zeit"-Headerzeile mit
Median-Sparkline und im Detail-Band den Preisverlauf des Tarifs plus
**Leistungsänderungen** zwischen zwei Zeitpunkten — via
`scripts/feature_history.py`, das jede extrahierte Tarif-Version
content-hash-basiert in `out/<sparte>/tariff-history/<stem>/YYYY-MM-DD.json` archiviert
(Hash exkludiert Pipeline-Metadaten → ein Re-Extract mit anderem Modell erzeugt
keine Phantom-Diffs).

## 🔬 Die Analyse-Pipeline

Alle Pfade je aktiver Sparte (`CHECK0R_VERTICAL`, Default aus der Registry):

```
data/<sparte>/inbox/*.pdf                 (Sammelordner: alle Downloads hier rein)
        │  scripts/intake.py   (stdlib)       Dateiname → Versicherer/Tarif/Doctype,
        ▼                                     URL-decode, Dry-Run + --apply
data/<sparte>/raw/<versicherer>/<tarif>/*.pdf   (lokal, gitignored)
        │  scripts/ingest.py   (uv + pypdf)   PDF → Text, Content-Hash-Dedup
        ▼
data/<sparte>/extracted/…txt + manifest.json    (lokal, gitignored)
        │  scripts/extract.py  (Modell)       je Tarif → strukturiertes JSON
        │    --filter   AVB auf Vergleichs-§§ trimmen (passt dann in kleine Modelle)
        │    --model    claude:opus | haiku | ollama:llama3.1:8b | mlx:…@http://…
        │    --repeat 3 Leistungen/Ausschlüsse über N Läufe unionieren
        │    --jobs 3   Tarife parallel extrahieren
        ▼
out/<sparte>/tariffs/*.json                (getrackt: reine LLM-Fakten; Beitrag/Stufe = null)
        │  scripts/regression.py (stdlib)     golden.json-Invarianten, exit≠0 bei Drift
        │  scripts/overlay.py   (stdlib)      Beitrag/Stufe/SB aus data/<sparte>/offers/ einmischen
        ▼                                       (kein Modell) → out/<sparte>/enriched/*.json (gitignored)
out/<sparte>/enriched/*.json  bzw.  out/<sparte>/tariffs/*.json
        │  scripts/render.py   (Modell)       Matrix + Vor/Nachteile (nimmt enriched, sonst pur)
        ▼
out/<sparte>/vergleich.md  +  out/<sparte>/index.html   (getrackt: Ergebnis)
```

- `ingest.py` hasht den **extrahierten Text**, nicht die Datei-Bytes — neu
  generierte Downloads mit anderem PDF-Zeitstempel kollabieren zur Dublette.
- `extract.py --filter` kürzt die oft 100+ Seiten lange AVB deterministisch
  (ohne Modell, `scripts/_filter.py`) auf die vergleichsrelevanten Passagen —
  aus ~200k Token werden ~75k, das passt ins Fenster kleiner/lokaler Modelle.
- `extract.py --repeat N` dämpft die Omissions-Varianz billiger Modelle:
  Leistungen/Ausschlüsse werden über N Läufe **unioniert** (Recall ~2–3×).

Und der modellfreie Markt-Zweig daneben:

```
config/verticals/<sparte>/check24-profile.json  (gitignored, PII)  dein Query verbatim
        │  scripts/check24_query.py                  Result-URL aus dem Profil bauen
        ▼
[CHECK24-Ergebnisseite]
        │  scripts/fetch_ratings.py --snapshot (Playwright)  ganze Liste scrapen
        │  scripts/harvest_docs.py --match …   (Playwright)  Doc-URLs live ernten
        │       Flow je Sparte aus vertical.json: RS = filestore-Bundles,
        │       hausrat/phv = Tarifdetails-Panel mit /file/-Links (flow=panel)
        ▼
data/<sparte>/snapshots/<datum>.json         (gitignored)  die Verlauf-Datenbank
data/<sparte>/sources/check24-documents.json (getrackt)    nur AVB/PIB-URLs, nie die PDFs
        │  scripts/fetch_docs.py --check              URLs erreichbar? (lädt nichts)
        │  scripts/fetch_docs.py <stem> --apply       on demand ziehen (parallelisiert)
        ▼
scripts/tui.py                                        alles interaktiv
```

## 🧠 Wo ein LLM zum Einsatz kommt — und wo bewusst nicht

Das ganze Tool ruft ein Modell an genau **drei** Stellen auf, alle über
`_providers.run()`:

| Stelle | Aufgabe | Unverzichtbar? |
|---|---|---|
| `extract.py` | PDF-Klartext → strukturiertes Fakten-JSON | **Ja — der Daseinszweck** |
| `eval.py` | Benchmark: Modelle gegen `golden.json` messen | Nein (Qualitätssicherung) |
| `render.py` | Vor-/Nachteile-Prosa für `vergleich.md` | Nein (Nice-to-have) |

Alles andere ist **bewusst modellfrei** und deterministisch: Scraping,
Magic-Find-Ranking, Snapshots, Preis-/Feature-Diffs, Coverage-Taxonomie,
`reconcile.py`, der `--filter`-Schritt.

**Warum genau bei `extract`:** Die AVB sind unstrukturierter juristischer
Fließtext, je Versicherer anders formatiert — keine API, kein einheitliches
Layout. Ein Regex/Parser müsste pro Versicherer neu geschrieben werden und
bräche bei jeder Umformulierung. Das Modell **normalisiert diese Vielfalt auf
ein festes Schema** — genau die Aufgabe, bei der ein LLM einem Regelwerk
überlegen ist:

```
REIN  ── ~200 000 Zeichen Jura-Deutsch (advocard-AVB) ───────────────────────
  IHR RECHTSSCHUTZ IM DETAIL. … §§ Geltungsbereich, Bausteine, Wartezeiten …

RAUS  ── ein schema-valider, versicherer-übergreifend vergleichbarer Record ──
  modules:    privat ✓, beruf ✓, verkehr ✓, wohnen ✓  (level: null — nicht genannt)
  coverage:   versicherungssumme "unbegrenzt", selbstbeteiligung "150/300 EUR wählbar"
  leistungen: 28 Einträge — "Anwaltsgebühren", "Mediationskosten bis 180 EUR/Std", …
```

Der Prompt zwingt auf **Fakten statt Raten** (`NEVER guess a number`; `level`
nur, wenn das Dokument die Stufe nennt). Weil Modelle trotzdem halluzinieren,
fängt die Pipeline es drumherum ab: `golden.json`-Invarianten
(`regression.py`), `--repeat N` mit Union, `reconcile.py` (Merge gegen HEAD,
nie Regression). `extract` ist **gecacht** (Prompt-Version + Modell + Filter +
Dokument-Hashes): einmal pro PDF-Version, danach kostenlos.

## 🤖 Modelle: Cloud & lokal

Jede Modell-Stufe nimmt einen Spec `[provider:]model[@endpoint]`
(`scripts/_providers.py`):

| Spec | Backend |
|---|---|
| `haiku`, `claude:opus`, `claude` | Anthropic `claude`-CLI (Cloud) |
| `ollama:llama3.1:8b` | Ollama, Default `http://localhost:11434/v1` |
| `mlx:<id>` | `mlx_lm.server`, Default `http://127.0.0.1:8080/v1` |
| `omlx:<id>` | oMLX, Default `http://127.0.0.1:8000/v1` (API-Key-gated) |
| `openai:<m>@http://host:port/v1` | beliebiger OpenAI-kompatibler Server |

Key-gated Server bekommen einen Bearer-Token aus der Umgebung
(`<PROVIDER>_API_KEY`, Fallback `OPENAI_API_KEY`) — nie im Code, nie geloggt.
Lokale Backends melden keine Kosten (`cost_usd = null`). Nur stdlib-`urllib`.

**Empfehlung:** `haiku --filter --repeat 3` ist der Sweet-Spot — getrimmte AVB
passt ins 200k-Fenster, ~7× günstiger als Opus, Korrektheit vergleichbar, die
Union über 3 Läufe gleicht die Recall-Varianz aus.

## 🧪 Benchmark (Modell- & Input-Vergleich)

`scripts/eval.py` jagt denselben Extraktions-Prompt **parallel** durch
beliebige Modelle und Input-Varianten und bewertet drei Achsen:

- **A Korrektheit** — Schema-Validität + Halluzinations-Guard (jeder behauptete
  Wert per Ziffern-Check gegen den *gefütterten* Quelltext) + Cross-Model-Agreement.
- **B Kosten** — `cost_usd` pro Call (`null` bei lokal).
- **C Performance** — Wall-clock, API-Dauer, Kontextfenster-Fit.

```sh
uv run scripts/eval.py --models haiku,omlx:Qwen3.5-9B-MLX-4bit --filter   # Cloud vs. lokal
uv run scripts/eval.py --models haiku,sonnet --filter --repeat 3 --save-summary
uv run scripts/eval.py --rescore              # Records offline neu bewerten (kostenlos)
```

`--save-summary` schreibt einen getrackten Digest nach
`benchmarks/<sparte>/results.md` (+`.json`) — den rendert der `[B]`-Tab als
Scorecard. **Das zentrale Ergebnis
hier:** der Input-Umfang schlägt die Modellwahl. Mit getrimmter AVB extrahieren
auch kleine lokale Modelle treu; die volle 174-Seiten-AVB sprengt jedes
200k-Fenster.

## 🛡️ Regression: merken wir, wenn die Extraktion bricht?

Ja. `benchmarks/<sparte>/golden.json` pinnt pro Golden-Tarif die **dokument-gegroundeten
Invarianten** — Fakten, die in den Unterlagen wirklich stehen, **und** Felder,
die bewusst `null` bleiben müssen (Beitrag, gewählte Stufe). Zusätzlich läuft
ein marktweiter Check über **alle** Records (Schema + beitrag-null).

```sh
uv run scripts/regression.py                   # aktive Sparte; pipeline.sh ruft es automatisch
uv run scripts/regression.py --all-verticals   # einmal je Registry-Sparte (CI)
```

Eine frisch gescaffoldete Sparte ohne `golden.json` bekommt trotzdem den
marktweiten Sweep — golden-los heißt „noch keine gepinnten Invarianten",
nicht „übersprungen".

So fällt ein Modell-/Prompt-Wechsel auf, der Felder fallen lässt, eine Stufe
halluziniert oder einen Beitrag erfindet — statt still schlechtere Records
auszuliefern.

## 🔒 Was getrackt wird (und was nicht)

Die Original-PDFs der Versicherer sind **fremdes Urheberrecht** und werden
**nicht** eingecheckt. Getrackt sind nur Tooling, Schema, die **abgeleiteten
Ergebnisse** (Fakten-JSON + Vergleichsprosa), die Tarif-History, der Benchmark
und die URL-Manifeste (Links, nie Inhalte).

| Bleibt lokal (gitignored; `<v>` = Sparte) | Warum |
|---|---|
| `data/<v>/raw/`, `data/<v>/inbox/`, `data/<v>/extracted/` | fremdes Urheberrecht (PDFs + Extrakte) |
| `data/<v>/snapshots/` | Scrape-Rohdaten |
| `config/verticals/<v>/check24-profile.json` | PII (Geburtsdatum, PLZ) |
| `data/<v>/offers/*`, `out/<v>/enriched/` | persönliche Beiträge/Stufen |
| `config/verticals/<v>/favorite-notes.json` | persönliche Notizen |

Keine Secrets, keine absoluten Pfade — API-Keys nur aus der Umgebung.

## 📎 Nebenpfad: persönliche Angebotsdaten (`data/<sparte>/offers/`)

AVB + Produktinfoblatt beschreiben die generische Produktlinie — **nicht** den
konkret gewählten Tarif. Beitrag, gewählte Stufe und SB kommen deshalb über
einen **modellfreien** Nebenpfad: `data/<sparte>/offers/<key>.json`
(`schema/<sparte>/offer.schema.json`, Doku: `data/<sparte>/offers/README.md`) →
`scripts/overlay.py` mischt sie **verbatim** ein → `out/<sparte>/enriched/`.

Die reinen Records bleiben eingefroren: `out/<sparte>/tariffs/` und `regression.py`
sehen den Offer nie — die Garantie „das Modell erfindet keinen Preis" bleibt
intakt. Overlay hat einen Containment-Self-Check (der Merge darf nur die
benannten Felder ändern) und validiert gegen das Schema.

<details>
<summary>🤔 <b>Prompt-Selbstverbesserung — warum (noch) nicht?</b></summary>

Ein autonomer Optimier-Loop (Prompt mutieren → scoren → besten behalten) lohnt
hier noch nicht: es gibt keine externe Ground-Truth, und die heuristischen
Metriken (Schema, Ziffern-Grounding, Agreement) sind *Proxys*, die ein
Optimierer austricksen kann — überall `null` schlägt den Halluzinations-Guard,
sagt aber nichts aus. Was stattdessen trägt und gebaut ist: `golden.json` als
hand-kuratierte Ground-Truth, `--repeat N` für die Varianz-Messung,
Cross-Model-Disagreement als Inspektions-Signal. Ein echter Optimierer lohnt
erst mit einer deutlich größeren kuratierten Golden-Menge — dann als A/B-Lauf
mit menschlicher Freigabe, nie als stiller Loop.

</details>

## 📐 Schema

`schema/<sparte>/tariff.schema.json` ist die Source-of-Truth dafür, welche
Merkmale in einer Sparte verglichen werden. Schema erweitern → `PROMPT_VERSION`
in `scripts/extract.py` erhöhen (invalidiert den Cache), Invarianten in
`benchmarks/<sparte>/golden.json` nachziehen. Eine ganz neue Sparte scaffoldet
`scripts/new_vertical.py` aus Probe-Evidenz (Beispiel-AVB + Result-Rows).

## ❤️ Support

Wenn dir das Projekt gefällt oder weiterhilft:
[ko-fi.com/bmabma](https://ko-fi.com/bmabma).

## License

GPL-3.0-or-later — siehe [LICENSE](LICENSE).
