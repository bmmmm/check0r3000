# check0r3000 — Rechtsschutzversicherung-Vergleich

Portable Pipeline, die Vertragsunterlagen von Rechtsschutzversicherungen (z. B. aus
einem check24-Vergleich) einliest, strukturierte Fakten extrahiert und eine
Vergleichsübersicht mit klaren Vor- und Nachteilen erzeugt.

Minimaler Stack: **Python via `uv`** (eine Dependency: `pypdf`) + die **`claude`-CLI**
für die Fakten-Extraktion und die vergleichende Synthese. Kein OCR, kein poppler,
keine schweren Frameworks.

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
        │  scripts/extract.py  (claude -p)    je Tarif → strukturiertes JSON
        ▼
out/tariffs/*.json                         (getrackt: Fakten)
        │  scripts/render.py   (claude -p)    Matrix + Vor/Nachteile
        ▼
out/vergleich.md  +  out/index.html        (getrackt: Ergebnis)
```

`scripts/ingest.py` hasht den **extrahierten Text**, nicht die Datei-Bytes — neu
generierte Downloads, die sich nur im PDF-Zeitstempel unterscheiden, kollabieren so
zum selben Hash und werden als Dublette gemeldet.

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
   ./pipeline.sh                 # ingest → extract → render
   ./pipeline.sh --model opus    # Modell für die LLM-Stufen überschreiben
   ```
   Einzelstufen mit Flags (Scripts sind direkt ausführbar via uv-Shebang;
   `uv run scripts/<name>.py …` ist gleichwertig):
   ```sh
   ./scripts/ingest.py
   ./scripts/extract.py --force      # Cache ignorieren
   ./scripts/render.py  --no-llm     # nur deterministische Matrix
   ```
3. Ergebnis: `out/vergleich.md` und `out/index.html`.

## Was die Unterlagen NICHT enthalten

AVB, Produktinformationsblatt und „Weitere Unterlagen" beschreiben die generische
Produktlinie eines Versicherers — **nicht** den konkret gewählten Tarif. Beitrag,
Selbstbeteiligung und die gewählten Bausteine stehen nur in der **Leistungsübersicht /
im persönlichen Angebot** bzw. in der **check24-Ergebnisliste**. Für einen echten
Tarifvergleich diese Quellen mitliefern (`leistungsuebersicht.pdf`) oder den Beitrag
in `out/tariffs/*.json` (`beitrag`) nachtragen.

## Schema

`schema/tariff.schema.json` ist die Source-of-Truth dafür, welche Merkmale verglichen
werden (Bausteine, Versicherungssumme, Selbstbeteiligung, Wartezeiten, Geltungsbereich,
Leistungen, Ausschlüsse, Beitrag). Schema erweitern → `PROMPT_VERSION` in
`scripts/extract.py` erhöhen, damit der Cache invalidiert.

## License

GPL-3.0-or-later — siehe [LICENSE](LICENSE).
