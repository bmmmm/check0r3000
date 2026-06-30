# check0r3000 — Rechtsschutzversicherung-Vergleich

Portable Pipeline, die Vertragsunterlagen von Rechtsschutzversicherungen (z. B. aus
einem check24-Vergleich) einliest, strukturierte Fakten extrahiert und eine
Vergleichsübersicht mit klaren Vor- und Nachteilen erzeugt.

Minimaler Stack: **Python via `uv`** + austauschbare Modell-Backends. Die Pipeline
selbst braucht nur `pypdf` (Text aus PDF); Fakten-Extraktion und vergleichende
Synthese laufen über ein Modell deiner Wahl — die **`claude`-CLI** (Cloud) oder ein
**lokales, OpenAI-kompatibles Backend** (Ollama, oMLX / `mlx_lm.server`, vLLM …).
Kein OCR, kein poppler, keine schweren Frameworks.

## Was getrackt wird (und was nicht)

Die Original-PDFs der Versicherer sind **fremdes Urheberrecht** und werden **nicht**
eingecheckt (`.gitignore`). Getrackt werden nur das Tooling, das Vergleichsschema, die
**abgeleiteten Ergebnisse** (Fakten als JSON + die Vergleichsprosa in `out/`) und der
**Benchmark** (`benchmarks/`: dokument-gegroundete Golden-Invarianten + Modell-Digest).
Damit bleibt das Repo öffentlich teilbar und trotzdem nachbaubar.

Keine Secrets, nur relative Pfade — bewusst von Anfang an.

## Pipeline

```
data/inbox/*.pdf                          (Sammelordner: alle Downloads hier rein)
        │  scripts/intake.py   (stdlib)       Dateiname → Versicherer/Tarif/Doctype,
        ▼                                     URL-decode, Dry-Run + --apply
data/raw/<versicherer>/<tarif>/*.pdf      (lokal, gitignored)
        │  scripts/ingest.py   (uv + pypdf)   PDF → Text, Content-Hash-Dedup
        ▼
data/extracted/…txt + manifest.json       (lokal, gitignored)
        │  scripts/extract.py  (Modell)       je Tarif → strukturiertes JSON
        │    --filter   AVB auf Vergleichs-§§ trimmen (passt dann in kleine Modelle)
        │    --model    claude:opus | haiku | ollama:llama3.1:8b | mlx:…@http://…
        ▼
out/tariffs/*.json                         (getrackt: reine LLM-Fakten; Beitrag/Stufe = null)
        │  scripts/regression.py (stdlib)     golden.json-Invarianten, exit≠0 bei Drift
        │  scripts/overlay.py   (stdlib)      Beitrag/Stufe/SB aus data/offers/ einmischen
        ▼                                       (kein Modell) → out/enriched/*.json (gitignored)
out/enriched/*.json  bzw.  out/tariffs/*.json
        │  scripts/render.py   (Modell)       Matrix + Vor/Nachteile (nimmt enriched, sonst pur)
        ▼
out/vergleich.md  +  out/index.html        (getrackt: Ergebnis)
```

`scripts/ingest.py` hasht den **extrahierten Text**, nicht die Datei-Bytes — neu
generierte Downloads, die sich nur im PDF-Zeitstempel unterscheiden, kollabieren so
zum selben Hash und werden als Dublette gemeldet.

`scripts/extract.py --filter` kürzt die oft 100+ Seiten lange **AVB** auf die
vergleichsrelevanten Passagen (Selbstbeteiligung, Versicherungssumme, Wartezeit,
Geltungsbereich, Modul-§§) — deterministisch, ohne Modell (`scripts/_filter.py`).
Das bringt eine 200k-Token-AVB auf ~75k herunter, sodass sie in das 200k-Fenster
kleiner/günstiger/lokaler Modelle passt, ohne die Fakten zu verlieren.

## Marktübersicht & TUI (ohne Modell)

Neben der Doku-Pipeline gibt es einen leichten, modellfreien Zweig, um den **ganzen
Markt** im Blick zu behalten — kein DB-Engine, nur Dateien:

```
config/check24-profile.json     (gitignored, PII)   dein Query verbatim
        │  scripts/check24_query.py                  Result-URL aus dem Profil bauen
        ▼                                             (--all-insurers, --provider, --show)
[CHECK24-Ergebnisseite]
        │  scripts/check24_scrape.js (DevTools)          Zeilen + Quell-PDF-URLs scrapen
        │  scripts/harvest_docs.py <stem> (Playwright)  Alternativ: URLs live ernten + Manifest mergen
        ▼
data/snapshots/<datum>.json       (gitignored)       scripts/snapshot.py: ganze Liste je Tag
data/sources/check24-documents.json (getrackt)       nur die AVB/PIB-URLs, nie die PDFs
        │  scripts/fetch_docs.py --check              URLs erreichbar? (lädt nichts; parallelisiert)
        │  scripts/fetch_docs.py <stem> --apply       on demand nach data/inbox/ ziehen
        ▼
scripts/tui.py                                        interaktiver Browser über alles (4 Tabs)
```

- **`scripts/snapshot.py`** ist die Änderungs-DB: ein datierter Snapshot pro Lauf,
  `--diff ALT NEU` zeigt Preisänderungen / neu / weg.
- **`scripts/fetch_docs.py --check`** prüft per HEAD/Range-Request, ob die gesicherten
  Dokument-URLs erreichbar sind — **ohne** ein (urheberrechtlich geschütztes) PDF zu laden.
  Intern parallelisiert (~4.8× schneller als seriell).
- **`scripts/harvest_docs.py <stem>`** erntet Quell-PDF-URLs für einen Tarif **live** aus
  der CHECK24-Ergebnisseite (Playwright), mergt sie content-hash-basiert in
  `data/sources/check24-documents.json` und lädt die PDFs direkt. Nützlich für Tarife, die
  kein gesichertes Manifest haben (z. B. KS/Auxilia, JURPRIVAT/S-Direkt). Im TUI mit
  **`[H]`** aufrufbar (Quell-URLs ernten + direkt analysieren in einem Schritt).
- **`scripts/tui.py`** (das einzige Skript mit `textual`-Dependency) bietet **vier Tabs**:
  **★ Favorites** (kuratierte Shortlist aus `config/favorites.json` mit Note/Preis/SB,
  Δ vs. Referenz, SB-Varianten und den gesicherten Dokument-URLs), **Market** (alle
  Tarife, sortier-/filterbar), **Verlauf** (`[l]`, s.u.) und **Vergleich** (`[x]`, s.u.).
  Jeder Tarif zeigt einen
  **Status** (`✓ analysiert · ↓ PDF lokal · ○ URLs · gelistet`) und zwei getrennte
  Bewertungs-Spalten: **Note** (CHECK24-Experten-Tarifnote) und **Bew.** (Kundenbewertung
  /5). `--selftest` prüft das Laden ohne UI; `--screenshot DIR` rendert jeden Tab als SVG.
- **Vergleich-Tab (`[x]`)** stellt alle analysierten Tarife nebeneinander (Spalten =
  Tarife, Referenz `[R]` ganz links als Basis): eine **Module-Matrix** (8 Lebensbereiche
  mit Stufen-Badge), eine **Deckungs-Matrix** (VS / SB / Wartezeit / Geltungsbereich /
  Laufzeit, je Feld kurz-normalisiert), und **Leistungen/Ausschlüsse-Matrizen**. Letztere
  lösen das Benennungs-Problem: `scripts/coverage_taxonomy.py` ordnet jeden Freitext-Eintrag
  über die kuratierte Taxonomie `config/coverage_taxonomy.json` einer kanonischen Kategorie
  zu (deterministisch, **ohne Modell-Call**), sodass dieselbe Leistung über Versicherer
  hinweg in einer Zeile steht, auch wenn sie anders heißt (`telefonische Rechtsberatung`
  ↔ `JuraTel®` ↔ `DMB-Hotline`). Default ist **kompakt** (nur die `✓/✗/~/—`-Matrix,
  Zeilen-Labels sauber gekürzt); **`[w]`** blendet den **Original-Wortlaut** je
  Versicherer ein (eine Zeile pro Tarif, hart gekürzt → kein Umbruch). Für den
  **vollständigen, ungekürzten Text** öffnet **`[t]`** ein Volltext-Modal: links die
  Kategorie-Liste (↑↓), rechts der ganze Wortlaut aller Tarife nebeneinander, umbrochen
  — inkl. der Tarife, die wegen der Terminalbreite nicht in die Matrix passen. Nicht
  zugeordnete Einträge landen sichtbar im **Sonstige**-Bucket (nie verworfen); `~` markiert
  Teil-Deckung (nur/eingeschr./außer/begrenzt). Der Vergleich ist eine **kuratierte
  Auswahl** (`compare_stems`), getrennt von den Favoriten: **`[a]`** (im Markt) nimmt den
  markierten Tarif in den Vergleich auf bzw. wieder heraus und analysiert ihn bei Bedarf
  automatisch; **`[c]`** (auf dem Vergleich-Tab) schaltet die Mitglieder im Block ein/aus.
  Fehlt der Key, startet der Vergleich als Kopie der Favoriten. Erweitern = ein Objekt an die
  Taxonomie anhängen (`coverage_taxonomy.py --selftest` pinnt die Cross-Tarif-Zuordnung).
  Der frühere Snapshot-Preis-Diff hängt nur noch an, sobald ein zweiter Snapshot existiert.
- **Verlauf-Tab (`[l]`)** vergleicht zwei Snapshots (`,`/`.` wählen, `[m]` Filter).
  Das Detail-Band zeigt neben Preis-/Stufenänderungen auch **Leistungsänderungen** —
  hinzugekommene und weggefallene Features — sofern analysierte Records in
  `out/tariff-history/<stem>/` für beide Zeitpunkte vorliegen (via `feature_history.py`).
- **Identität über den `stem`**: jeder Tarif hat einen kanonischen `stem`
  (`<versicherer>__<tarif>`, aus `data/sources/check24-documents.json`). TUI-Lookup,
  Pipeline-Output und Doc-Manifest hängen alle daran — `[g]` schreibt direkt nach
  `data/raw/<stem>/`, sodass `out/.../<stem>.json` garantiert gefunden wird.
- **`[?]` zeigt alle Shortcuts** (der Footer nur die wichtigsten). Zentrale Tasten:
  **`[d]`** Detail-Band unter der Tabelle ein/aus (Default aus → volle Tabellenbreite);
  **`[g]`** lädt + analysiert nach Bestätigung (Pipeline `fetch_docs --into-raw → ingest
  → extract` im Hintergrund; Modell per `CHECK0R_ANALYZE_MODEL`, Default `claude`);
  **`[G]`** nur analysieren ohne Download (`ingest → extract`), wenn die PDFs schon unter
  `data/raw/<stem>/` liegen; **`[H]`** Quell-URLs **live ernten + direkt analysieren**
  (Playwright; für Tarife ohne gesichertes Manifest); **`[o]`** öffnet die Quelldokumente
  online (Browser) oder als lokale PDFs (`data/raw/<stem>/`); **`[R]`** setzt die markierte
  Zeile als Δ-Referenz; **`[u]`** Favorit an/aus; **`[c]`** Tarif aus dem Vergleich
  aus-/einblenden; **`[D]`** lokale Daten löschen (mit Umfang-Auswahl); **`[b]`** baut
  die CHECK24-URL. Tarife ohne gesicherte URLs verweisen auf den Browser-Schritt
  „Tarifdetails". Ein analysierter Favorit zeigt im Detail-Band den **vollen Record**
  (Module/Deckung/Leistungen/Ausschlüsse/Besonderheiten), nicht nur die Preis-Zusammenfassung.
- **TUI-Architektur:** `tui.py` ist ein ~270-Zeilen-Entry-Point, der an vier sibling-Module
  delegiert: `tui_data.py` (Textual-freie Daten-/Lade-Schicht, unabhängig testbar via
  `python3 tui_data.py --selftest`), `tui_format.py` (Rendering-Helpers), `tui_screens.py`
  (Widget-Definitionen, Screens) und `tui_app.py` (App-Klasse, Bindings, Actions).

```sh
uv run scripts/check24_query.py --all-insurers   # Result-URL für alle Versicherer
uv run scripts/snapshot.py rows.psv              # Snapshot bauen   (--diff alt neu)
uv run scripts/fetch_docs.py --check             # sind alle Doc-URLs erreichbar?
uv run scripts/tui.py                            # interaktiver Vergleich
```

`scripts/tui.py` hat eine `uv`-Shebang und löst seine Datenpfade relativ zur eigenen
(aufgelösten) Position auf — ein Symlink auf dem PATH startet die TUI also aus jedem
Verzeichnis:

```sh
ln -s "$(pwd)/scripts/tui.py" ~/.local/bin/check0r3000   # einmalig, ~/.local/bin auf PATH
check0r3000                                              # danach von überall
```

## Nachbauen / einen Versicherer hinzufügen

Voraussetzungen: [`uv`](https://docs.astral.sh/uv/) und die `claude`-CLI auf dem PATH.

1. PDFs in den Sammelordner `data/inbox/` bekommen (Original-Dateinamen von check24
   können bleiben) — entweder selbst hineinkopieren, oder per Pfad importieren lassen:
   ```sh
   # Variante A: Pfade direkt übergeben (kopiert in die Inbox; --move zum Verschieben)
   ./scripts/intake.py ~/Downloads/Allgemeine_Versicherungsbedingungen_*.pdf
   # Variante B: Dateien selbst nach data/inbox/ legen

   # dann einsortieren:
   ./scripts/intake.py            # Dry-Run: zeigt den Sortierplan
   ./scripts/intake.py --apply    # verschiebt nach data/raw/<versicherer>/<tarif>/
   ```
   Dateinamen mit Leerzeichen/Sonderzeichen/URL-Encoding (`%C2%B0`) werden korrekt
   normalisiert. Pfade mit Leerzeichen in der Shell quoten.
   `intake.py` erkennt Dokumenttyp und Versicherer am Dateinamen. Unbekannte
   Versicherer in `KNOWN_INSURERS` (in `scripts/intake.py`) ergänzen. Die eigentliche
   Vergleichsgrundlage je Tarif ist die `leistungsuebersicht.pdf` (Beitrag, Bausteine,
   Selbstbeteiligung) — wenn vorhanden, mit in die Inbox legen.

   Alternativ Dateien direkt unter `data/raw/<versicherer>/<tarif>/<doctype>.pdf`
   ablegen (Stem = `doctype`).
2. Pipeline laufen lassen:
   ```sh
   ./pipeline.sh --model haiku --filter        # empfohlen: günstig + AVB getrimmt
   ./pipeline.sh --model opus                   # volle AVB (braucht großes Fenster)
   ./pipeline.sh --model ollama:llama3.1:8b --filter   # lokal, kostenlos
   ```
   Einzelstufen mit Flags (Scripts sind direkt ausführbar via uv-Shebang;
   `uv run scripts/<name>.py …` ist gleichwertig):
   ```sh
   ./scripts/ingest.py
   ./scripts/extract.py --force --model haiku --filter   # Cache ignorieren
   ./scripts/overlay.py                                   # Beitrag/Stufe aus data/offers/ (kein Modell)
   ./scripts/render.py  --no-llm                          # nur deterministische Matrix
   ```
3. Ergebnis: `out/vergleich.md` und `out/index.html`.

## Wo ein LLM zum Einsatz kommt — und wo bewusst nicht

Das ganze Tool ruft ein Modell an genau **drei** Stellen auf, alle über
`_providers.run()` → `claude -p … --output-format json` (bzw. den konfigurierten
OpenAI-kompatiblen Endpoint):

| Stelle | Aufgabe | Unverzichtbar? |
|---|---|---|
| `extract.py` | PDF-Klartext → strukturiertes Fakten-JSON | **Ja — der Daseinszweck** |
| `eval.py` | Benchmark: Modelle gegen `golden.json` messen | Nein (Qualitätssicherung) |
| `render.py` | `synthesize_pros_cons`: Vor-/Nachteile-Prosa für `vergleich.md` | Nein (Nice-to-have) |

Alles andere ist **bewusst modellfrei** und deterministisch: Scraping (DOM-Parsing),
das Magic-Find-Ranking (reine Arithmetik über die Records), Snapshots, Feature-Diffs,
`reconcile.py`, der Harvest und der `--filter`-Schritt. Das Modell wird nur dort
eingesetzt, wo es das einzige Werkzeug ist, das funktioniert.

**Warum nur bei `extract`:** Die AVB sind unstrukturierter juristischer Fließtext, je
Versicherer anders formatiert — keine API, kein einheitliches Layout. Derselbe Fakt
steht mal als Paragraph (`§30(4): Verkehrs-Rechtsschutz ist Kernbereich`), mal als
Tabelle, mal als Aufzählung. Ein Regex/Parser müsste pro Versicherer neu geschrieben
werden und bräche bei jeder Umformulierung. Das Modell **normalisiert diese Vielfalt
auf ein festes Schema** — genau die Aufgabe, bei der ein LLM einem Regelwerk überlegen
ist.

```
REIN  ── ~200 000 Zeichen Jura-Deutsch (advocard-AVB) ───────────────────────
  IHR RECHTSSCHUTZ IM DETAIL. … §§ Geltungsbereich, Bausteine, Wartezeiten …

RAUS  ── ein schema-valider, versicherer-übergreifend vergleichbarer Record ──
  modules:    privat ✓, beruf ✓, verkehr ✓, wohnen ✓  (level: null — nicht genannt)
  coverage:   versicherungssumme "unbegrenzt", selbstbeteiligung "150/300 EUR wählbar"
  leistungen: 28 Einträge — "Anwaltsgebühren", "Mediationskosten bis 180 EUR/Std", …
```

Der `INSTRUCTION`-Prompt zwingt dabei auf **Fakten statt Raten** (`NEVER guess a
number`; `level` nur, wenn das Dokument die Stufe nennt). Weil ein Modell trotzdem
halluziniert, fängt die Pipeline drumherum das ab: `golden.json`-Invarianten
(`regression.py`), `--repeat N` mit Union über mehrere Läufe und `reconcile.py`
(Merge gegen HEAD, nie Regression).

`extract` ist zudem **gecacht** (`_input_hash` aus Prompt-Version + Modell + Filter +
Dokument-Hashes): einmal pro PDF-Version, danach kostenlos. `eval` und die
`render`-Prosa sind optional — die Coverage-Matrix im Vergleich-Tab ist deterministisch,
nur die „Vor-/Nachteile"-Sektion ruft ein Modell. Ohne `extract` gäbe es gar keine Daten.

## Modelle: Cloud & lokal

Jede Stufe, die ein Modell nutzt (`extract`, `render`, `eval`), nimmt einen
**Modell-Spec** `[provider:]model[@endpoint]` (siehe `scripts/_providers.py`):

| Spec | Backend |
|---|---|
| `haiku`, `claude:opus`, `claude` | Anthropic `claude`-CLI (Cloud) |
| `ollama:llama3.1:8b` | Ollama, OpenAI-kompatibel, Default `http://localhost:11434/v1` |
| `mlx:<id>` | `mlx_lm.server`, Default `http://127.0.0.1:8080/v1` |
| `omlx:Qwen3.5-9B-MLX-4bit` | oMLX, Default `http://127.0.0.1:8000/v1` (API-Key-gated) |
| `openai:<m>@http://host:port/v1` | beliebiger OpenAI-kompatibler Server |

Key-gated lokale Server (z. B. oMLX) bekommen einen Bearer-Token aus der Umgebung:
`OMLX_API_KEY` (allgemein `<PROVIDER>_API_KEY`, Fallback `OPENAI_API_KEY`) — nie im Code,
nie geloggt; in `~/.env` setzen. Server ohne Key-Pflicht laufen ohne Header. Lokale
Backends melden keine Kosten (`cost_usd = null`). Nur stdlib (`urllib`), keine
zusätzliche Dependency.

## Benchmark (Modell- & Input-Vergleich)

`scripts/eval.py` ist ein eigenständiges Bench-Tool: es jagt denselben
Extraktions-Prompt **parallel** durch beliebige Modelle und Input-Varianten und
bewertet drei Achsen — gedacht zum Nutzen, Optimieren und Vergleichen, nicht nur
für dieses Repo:

- **A Korrektheit** — JSON-Schema-Validität + Halluzinations-Guard (jeder
  behauptete Wert wird per Ziffern-Check gegen den *gefütterten* Quelltext
  verifiziert) + Cross-Model-Agreement bei den Modulen.
- **B Kosten** — `cost_usd` pro Call (aus dem Provider; `null` bei lokal).
- **C Performance** — Wall-clock + API-Dauer + Kontextfenster-Fit.

```sh
uv run scripts/eval.py                                   # haiku/sonnet/opus, volle Docs
uv run scripts/eval.py --models haiku --filter           # getrimmte AVB
uv run scripts/eval.py --models haiku,omlx:Qwen3.5-9B-MLX-4bit --filter   # Cloud vs. lokal
uv run scripts/eval.py --docs produktinfoblatt           # nur Teil-Dokumente
uv run scripts/eval.py --models haiku,sonnet --filter --repeat 3 --save-summary  # Varianz + durable Digest
uv run scripts/eval.py --rescore                          # Records offline neu bewerten (kostenlos)
```

Ergebnisse je Lauf landen in `tmp/eval/` (gitignored, weil konto-/zeitspezifisch).
Mit `--save-summary` schreibt `eval.py` zusätzlich einen **getrackten** Digest nach
`benchmarks/results.md` (+`.json`): die Korrektheits-Achse ist reproduzierbar, Kosten und
Latenz sind als Momentaufnahme markiert. `--repeat N` führt jede Zelle N-mal aus und zeigt
die **Run-to-Run-Varianz** — genau die Schwäche, die einen Single-Run-Vergleich täuscht.
`eval.py` braucht zusätzlich `jsonschema` (via uv-Inline-Deps automatisch).

**Was der Benchmark hier gezeigt hat:** Der Input-Umfang schlägt die Modellwahl.
Mit getrimmter AVB extrahieren auch kleine/lokale Modelle die Vergleichsfakten
treu und ~7× günstiger als Opus; die volle 174-Seiten-AVB sprengt dagegen das
200k-Fenster von Haiku/Sonnet. Günstige Modelle schwanken aber run-to-run stärker
in der Vollständigkeit — für stabile Ergebnisse `--model sonnet` oder mehrfach
laufen lassen.

## Regression: merken wir, wenn die Extraktion bricht?

Ja. `benchmarks/golden.json` pinnt pro Tarif die **dokument-gegroundeten Invarianten**
— die Fakten, die in den Unterlagen wirklich stehen (Module enthalten, `unbegrenzt`,
Wartezeit 3, ADVOCARD-Selbstbeteiligung 150/300 …) **und** die, die dort bewusst *nicht*
stehen und `null` bleiben müssen (Beitrag, gewählte Stufe). Jede Invariante nennt ihren
Beleg; jede ist gegen den echten Quelltext geprüft.

```sh
uv run scripts/regression.py   # prüft out/tariffs/*.json gegen golden.json, exit≠0 bei Drift
```

`pipeline.sh` ruft den Check am Ende automatisch auf (nicht-fatal, aber laut). So fällt ein
Modell-/Prompt-Wechsel auf, der Felder fallen lässt, eine Stufe halluziniert, die Identität
auf `null` setzt oder einen Beitrag erfindet — statt still einen schlechteren
`out/tariffs/*.json` auszuliefern. Genau das hat der Check beim Bau gefangen: zwei
schema-Defekte im damaligen Output (Pipeline-Meta-Keys + `beitrag: null` statt Objekt).
Schema ändern → Invarianten in `golden.json` nachziehen.

## Feature-History (Verlauf-Diff)

`scripts/feature_history.py` archiviert jede extrahierte Tarif-Version content-hash-basiert
in `out/tariff-history/<stem>/YYYY-MM-DD.json`. Der Hash schließt Pipeline-Metadaten und
persönliche Prämien aus — ein Re-Extract mit anderem Modell oder Prompt erzeugt keine
Phantom-Diffs.

`scripts/extract.py` ruft `archive_version()` nach jedem erfolgreichen Schreiben automatisch
auf. Die API im Überblick:

| Funktion | Was |
|---|---|
| `archive_version(stem, record)` | Schreibt idempotent (doppeltes Schreiben am selben Tag ist ein No-Op) |
| `state_as_of(stem, date)` | Letzter archivierter Record ≤ date |
| `diff_features(old, new)` | Hinzugekommene / weggefallene Leistungen als Dict |
| `backfill(stem)` | Backfilliert ältere Records aus git-History |

Der **Verlauf-Tab** der TUI nutzt `state_as_of` + `diff_features`, um im Detail-Band neben
Preis- und Stufenänderungen eine **Leistungsänderungs-Sektion** anzuzeigen. Die Dateien in
`out/tariff-history/` sind getrackt (eigene abgeleitete Fakten, kein Urheberrechtsproblem).

## Prompt-Selbstverbesserung — sinnvoll?

Ein **autonomer** Optimier-Loop (Prompt mutieren → scoren → besten behalten) lohnt hier
**noch nicht**: es gibt keine externe Ground-Truth, und die heuristischen Metriken (Schema,
Ziffern-Grounding, Cross-Model-Agreement) sind *Proxys*, die ein Optimierer austricksen kann
— überall `null` schlägt den Halluzinations-Guard, sagt aber nichts aus. Bei nur zwei
Tarifen würde er zusätzlich overfitten. Der Prompt ist außerdem schon durch echte
Fehlermodi hand-gehärtet (Stufe-Halluzination, erfundene Provenance, fallengelassene Felder).

Was stattdessen trägt — und gebaut ist:

- **`golden.json`** ist die hand-kuratierte Ground-Truth, die den Heuristiken fehlt; die
  Regression-Prüfung fängt Verschlechterungen automatisch.
- **`--repeat N`** misst die Run-to-Run-Varianz billiger Modelle und legt sie im Digest ab.
- **Cross-Model-Disagreement** markiert die unsicheren Stellen (z. B. ARAG `internet_web`)
  als Inspektions-Signal, statt sie zu verstecken.

Ein echter Optimierer lohnt erst ab ~10–20 Tarifen mit kuratierter Golden-Menge — dann als
A/B-Lauf zweier Prompt-Versionen mit menschlicher Freigabe, nie als stiller Loop.

## Was die Unterlagen NICHT enthalten — und der strukturierte Nebenpfad

AVB, Produktinformationsblatt und „Weitere Unterlagen" beschreiben die generische
Produktlinie eines Versicherers — **nicht** den konkret gewählten Tarif. Beitrag,
Selbstbeteiligung und die gewählte Leistungsstufe (`modules.*.level`,
Basis/Komfort/Premium) stehen nur in der **Leistungsübersicht / im persönlichen
Angebot** bzw. in der **check24-Ergebnisliste**. Die Extraktion lässt sie deshalb
bewusst `null` (und `golden.json` pinnt sie `null`, damit das Modell sie nie errät) —
ohne diese Quelle ist das Ergebnis ein **Deckungs-**, kein **Preis- oder
Stufen-Vergleich**.

Diese Fakten kommen über einen **eigenen, modellfreien Nebenpfad** rein: eine
strukturierte Datei `data/offers/<key>.json` (Format: `schema/offer.schema.json`,
Vorlage: `data/offers/_example.json`, Doku: `data/offers/README.md`).
`scripts/overlay.py` validiert sie, mischt Beitrag/Stufe/Selbstbeteiligung **verbatim**
auf das reine LLM-Record und schreibt `out/enriched/<key>.json`; `render` nimmt dann
das enriched-Record statt des puren.

- **Kein Modell** — jeder Wert wird wörtlich aus der von dir kontrollierten Datei kopiert.
- **Reine Records bleiben eingefroren** — `out/tariffs/` und `regression.py` sehen den
  Offer nie; die Garantie „das Modell erfindet keinen Preis / keine Stufe" bleibt intakt.
- **Containment-Self-Check** — overlay prüft, dass der Merge ausschließlich Beitrag, die
  benannten Stufen, die benannte SB und genau einen Provenance-Eintrag ändert, und
  validiert das Ergebnis gegen `tariff.schema.json`. Eine Stufe auf einem nicht gedeckten
  Modul wird abgelehnt (das Angebot widerspräche der AVB). `scripts/overlay.py --check`
  re-validiert bestehende enriched-Records.
- **Privacy** — der reale Beitrag ist persönlich: `data/offers/*` (außer Vorlage/README)
  und `out/enriched/` sind gitignored; nur die Vorlage mit Fake-Werten ist getrackt.

## Schema

`schema/tariff.schema.json` ist die Source-of-Truth dafür, welche Merkmale verglichen
werden (Bausteine, Versicherungssumme, Selbstbeteiligung, Wartezeiten, Geltungsbereich,
Leistungen, Ausschlüsse, Beitrag). Schema erweitern → `PROMPT_VERSION` in
`scripts/extract.py` erhöhen, damit der Cache invalidiert.

## License

GPL-3.0-or-later — siehe [LICENSE](LICENSE).
