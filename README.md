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
eingecheckt (`.gitignore`). Getrackt werden nur das Tooling, das Vergleichsschema und
die **abgeleiteten Ergebnisse** (Fakten als JSON + die Vergleichsprosa in `out/`).
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
out/tariffs/*.json                         (getrackt: Fakten)
        │  scripts/render.py   (Modell)       Matrix + Vor/Nachteile
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
   ./scripts/render.py  --no-llm                          # nur deterministische Matrix
   ```
3. Ergebnis: `out/vergleich.md` und `out/index.html`.

## Modelle: Cloud & lokal

Jede Stufe, die ein Modell nutzt (`extract`, `render`, `eval`), nimmt einen
**Modell-Spec** `[provider:]model[@endpoint]` (siehe `scripts/_providers.py`):

| Spec | Backend |
|---|---|
| `haiku`, `claude:opus`, `claude` | Anthropic `claude`-CLI (Cloud) |
| `ollama:llama3.1:8b` | Ollama, OpenAI-kompatibel, Default `http://localhost:11434/v1` |
| `mlx:<id>` | oMLX / `mlx_lm.server`, Default `http://127.0.0.1:8080/v1` |
| `openai:<m>@http://host:port/v1` | beliebiger OpenAI-kompatibler Server |

Lokale Backends melden keine Kosten (`cost_usd = null`). Nur stdlib (`urllib`),
keine zusätzliche Dependency.

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
uv run scripts/eval.py --models haiku,ollama:llama3.1:8b  # Cloud vs. lokal
uv run scripts/eval.py --docs produktinfoblatt           # nur Teil-Dokumente
uv run scripts/eval.py --rescore                          # Records offline neu bewerten (kostenlos)
```

Ergebnisse landen in `tmp/eval/` (gitignored, weil konto-/zeitspezifisch).
`eval.py` braucht zusätzlich `jsonschema` (via uv-Inline-Deps automatisch).

**Was der Benchmark hier gezeigt hat:** Der Input-Umfang schlägt die Modellwahl.
Mit getrimmter AVB extrahieren auch kleine/lokale Modelle die Vergleichsfakten
treu und ~7× günstiger als Opus; die volle 174-Seiten-AVB sprengt dagegen das
200k-Fenster von Haiku/Sonnet. Günstige Modelle schwanken aber run-to-run stärker
in der Vollständigkeit — für stabile Ergebnisse `--model sonnet` oder mehrfach
laufen lassen.

## Was die Unterlagen NICHT enthalten

AVB, Produktinformationsblatt und „Weitere Unterlagen" beschreiben die generische
Produktlinie eines Versicherers — **nicht** den konkret gewählten Tarif. Beitrag,
Selbstbeteiligung und die gewählten Bausteine stehen nur in der **Leistungsübersicht /
im persönlichen Angebot** bzw. in der **check24-Ergebnisliste**. Für einen echten
Tarifvergleich diese Quellen mitliefern (`leistungsuebersicht.pdf`) oder den Beitrag
in `out/tariffs/*.json` (`beitrag`) nachtragen.

Konkret heißt das: die **gewählte Leistungsstufe** (`modules.*.level`,
Kompakt/Komfort/Premium) bleibt absichtlich `null`, solange keine Leistungsübersicht
vorliegt — die Dokumente listen die Stufen nur als wählbare Optionen, nicht die für
diesen Tarif gebuchte. Ohne diese Quelle ist das Ergebnis ein **Deckungs-**, kein
**Preis- oder Stufen-Vergleich**.

## Schema

`schema/tariff.schema.json` ist die Source-of-Truth dafür, welche Merkmale verglichen
werden (Bausteine, Versicherungssumme, Selbstbeteiligung, Wartezeiten, Geltungsbereich,
Leistungen, Ausschlüsse, Beitrag). Schema erweitern → `PROMPT_VERSION` in
`scripts/extract.py` erhöhen, damit der Cache invalidiert.

## License

GPL-3.0-or-later — siehe [LICENSE](LICENSE).
