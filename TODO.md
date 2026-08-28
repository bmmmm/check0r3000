# TODO

## Multi-Vertical — nächste Schritte (Stand 2026-08-28)

Grundlage: der gelandete Multi-Vertical-Umbau (`fc25545..696ea15` — Namespace via
`_vertical.py`, Konstanten als Daten, `[S]`-Sparten-Auswahl, `new_vertical.py`,
Hausrat + Privathaftpflicht als `experimental` mit echten Scans/AVB/Extracts).
Reihenfolge-Empfehlung: A1+A4 → A2 → B → C → D.

### A. E2E & Test-Härtung

- [ ] **A1 E2E je neuer Sparte lokal:** `CHECK0R_VERTICAL=<v> ./update-all.sh
      --no-scan` für `hausrat` + `privathaftpflicht` (Hosts sind seit 2026-08-28 in
      der Sandbox-Allowlist): beweist fetch_docs über die `/file/`-URLs, Zweitlauf =
      alles cached / 0 USD, Report. Dazu TUI-`[g]` auf einem Hausrat-Tarif
      (Manifest-Einträge liegen vor).
- [ ] **A2 Selftests sparten-parametrisieren:** `magic.py --selftest` trägt
      RS-Hardcodes (8/8-Breadth-Asserts Z. ~613/743, Baustein-Key `"privat"`
      Z. ~764) — synthetische Fixtures aus `_modules.module_keys()` ableiten; danach
      die CI-Selftest-Zeilen analog `regression.py --all-verticals` über die
      Registry loopen (tui_data, magic, coverage_taxonomy je Sparte).
- [ ] **A3 tui_test:** optionaler local-only Case „echte Sparte" (hausrat mit echten
      Daten statt Fixture; Skip wenn kein Snapshot — CI-robust).
- [ ] **A4 Ersten GitHub-CI-Lauf** mit `--all-verticals` kontrollieren
      (Fresh-Clone-Pfade: kein data/extracted, keine Snapshots).

### B. Harvest-Generalisierung (Voraussetzung für [F]/[H]/Scan in neuen Sparten)

- [ ] **B1 `harvest_docs.py` auf Panel-Flows generalisieren:** Sparten-Spec nach
      `vertical.json` (card_sel, Expander, Docs-Tab, doc_link_filter, kind-Mapping
      `terms_combined`/`infos_static`/…); RS-`/filestore/`-Pfad byte-identisch
      halten. Basis: `scripts/probe/mini_harvest.py` (Probe-Grade, dort gerettet).
      Gotchas stehen im Docstring + Memory: PHV-Expander ist der
      „Tarifdetails"-TEXT-Link (Wrap-Div togglet nichts), Links liegen nach
      Expansion ohne Tab-Klick im DOM; Hausrat braucht den „Dokumente"-Tab als
      Real-Click (JS-click feuert den Vue-Handler nicht); „weiter" = Login-Wall.
- [ ] **B2 Scan produktiv je Sparte:** Profil aus `check24-profile.example.json`
      kopieren, `fetch_ratings`/`fetch_prices` je Sparte prüfen, dann `[U]`-E2E.

### C. Kuration experimental → production (je Sparte)

- [ ] **C1 Draft-Module reviewen** (hausrat 12 / phv 10 — mergen/umbenennen wo
      sinnvoll), `included` wieder strict `boolean` im Schema.
- [ ] **C2 Taxonomy verfeinern:** Hausrat-Records treffen aktuell nur ~5/15
      Kategorien — Synonyme aus den extrahierten `leistungen` nachziehen.
- [ ] **C3 `benchmarks/<v>/golden.json`:** 1–2 Tarife handverifizieren (Records für
      AO NOW Balance 2025 / Allianz Direct DIRECT liegen vor) + golden_pins.
- [ ] **C4 Mehr Tarife analysieren** (nach B1 via Markt-Scan `[F]`),
      external-ratings kuratieren, dann Status-Flip auf `production` in
      `config/verticals.json`.

### D. Doku & Hygiene

- [ ] **D1 `README.md`:** 12 veraltete Pfad-Referenzen (data/raw, out/tariffs, …)
      → `/readme-sync`.
- [ ] **D2 Worktree `multi-vertical` entfernen** (gemergt; beim Sessionende).

---

# Effizienz-Plan (abgeschlossen)

Stand: 2026-08-03. Grundlage sind Messungen aus dem Voll-Refresh dieses Tages,
nicht Schätzungen — die Zahlen unten sind reproduzierbar.

## Ausgangslage

**Der teure Scan liefert fast nie neue Information.** Änderungen zwischen den
fünf Snapshots in `data/snapshots/`:

| Übergang | Preis | Bewertung | Tarifnote |
|---|---|---|---|
| 06-24 → 06-26 | 9 | 214 (Erstbefüllung) | 4 |
| 06-26 → 07-10 | 4 | 16 | 3 |
| 07-10 → 07-11 | 0 | 0 | 0 |
| 07-11 → 08-03 | 0 | 0 | 11 |

13 Preisänderungen bei 214 Tarifen in sechs Wochen — und trotzdem läuft für
jeden Lauf headless Chromium mit Sandbox-Bypass.

**Preise brauchen keinen Browser.** Ein reiner HTTP-GET auf die Ergebnisseite
liefert ~4,4 MB in **2,7 s**. Ein Parser-Prototyp reproduziert daraus
**214/214 Zeilen exakt** (Versicherer, Produkt, Preis, Tarifnote) — nachdem die
Note wie im Playwright-Pfad auf eine Nachkommastelle normalisiert wird (`1` →
`1,0`). Selbstbeteiligung, Wartezeit und Deckungssumme stehen ebenfalls im
server-gerenderten HTML. Nur `bewertung` / `bewertung_anzahl` sind Vue-Platzhalter
und brauchen echte Hydration.

**Der Token-Fresser ist ARAG.** 27 AVB-Texte: 12,73 M Zeichen roh → 5,02 M nach
`_filter` (39 %). Die sechs ARAG-Dokumente sind je ~711 k Zeichen — 6× der
Durchschnitt — und werden mit 45 % am **schlechtesten** getrimmt. Deren Payload
von 391 446 Zeichen sprengt den Extract-Call (Abbruch mit 0 Tokens, 0 Kosten).

**Der Host kappt große Transfers generell.** Nicht nur PDFs: auch das SSR-HTML
brach beim ersten Versuch bei 4,0 von 4,4 MB ab. Range-Requests werden ignoriert
(200 statt 206, kein `Accept-Ranges`) — Resume ist unmöglich, nur Retry hilft.

## Umgesetzt am 2026-08-03

Alle fünf geplanten Maßnahmen sind erledigt. Was jeweils daraus wurde:

**1. Preis-Scan ohne Browser** — `scripts/fetch_prices.py` (`4e34029`).
Läuft in-sandbox in 3–7 s statt Chromium plus Bypass. Gegen den
Playwright-Snapshot desselben Tages validiert: 214/214 Zeilen, alle von
`snapshot.py` gespeicherten Felder identisch. Bewertungen werden aus dem letzten
Snapshot übernommen und mit `bewertung_stand` gestempelt.
`fetch_ratings.py` bleibt der Voll-Scan für die Bewertungen.

**2. Plausibilitäts-Guard** — `035e6d6`. Ein Record zählt nur als degeneriert,
wenn **beide** Identitätsfelder den Slug echoen. Die ODER-Form hätte gesunde
Records mitgenommen, deren Tarifname legitim dem Slug gleicht (`Klaro`,
`JURPRIVAT`) — gemessen über alle 26 Records: ODER markiert 3, UND genau den
einen kaputten.

**3. Record-Aktualitäts-Invariante** — `1ce6ceb`. Vergleicht `record.sources`
gegen `data/extracted/manifest.json`; übersprungen, wenn die Datei fehlt
(gitignored, also in CI und frischen Clones legitim abwesend).

**4. Snapshot-Dedup** — `87d9952`. Der Vergleich lässt `position` aus: die beiden
Scan-Pfade sortieren unterschiedlich, und eine Umsortierung ist kein Preisereignis.
`--force` überschreibt.

**5. ARAG-Filter** — `d5a0337`. `filter_text` nimmt ein `max_chars`-Budget und
verengt das Kontextfenster (4 → 1) nur so weit, bis es passt. Budget 250k, aus
der Messung abgeleitet statt gerundet: 228k geht durch, 321k nicht. Bindet
dadurch allein auf die ARAG-Familie (321k → 239k); die übrigen 20 AVB bleiben
bit-identisch und behalten Kontext wie Cache. `FILTER_VERSION` bewusst **nicht**
erhöht — das hätte alle 26 Records für Geld neu extrahiert.

**Altlasten erledigt** (`b4a7944`): Beide re-geharvesteten ARAG-Tarife sind aus
ihren korrekten Dokumenten neu extrahiert. `arag__komfort-2026` liest jetzt
`ARAG SE` statt `ÖRAG Rechtsschutzversicherungs-AG`. Beide über den Twin-Index
aufgelöst, ohne bezahlte Modell-Calls. `regression.py` ist auf allen drei Gates grün.

## Nachgezogen am 2026-08-03 (Optimierungsrunde)

**AVB-Budget von 250k auf 290k korrigiert** (`340d099`). Das erste Budget war zu
aggressiv — Trimmen kostet Recall, und der Preis war messbar:

| AVB | Payload | Ergebnis |
|---|---|---|
| 321k | 391k | abgelehnt (0 Tokens, 0 Kosten) |
| 283k | 350k | akzeptiert, **54 Leistungen** |
| 239k | 310k | akzeptiert, **26 Leistungen** |

Ein Dokument, das passt, nützt nichts, wenn die Klauseln herausgeschnitten wurden.
Das Budget gehört so hoch wie das Modell es noch annimmt.

**`arag__premium-familienrecht-2026` repariert** (`340d099`). Der Record, den der
Guard zwar markieren, aber nicht heilen konnte. Liest jetzt
`ARAG SE / Aktiv-Rechtsschutz Premium für Privatpersonen`, 54 Leistungen, 3/3 Runs.

**`_sections` bleibt** (`ad5aaae`). Die frühere Notiz „faktisch wirkungslos" war aus
vier AVB-Stichproben gezogen und hielt nicht: für AVBs gewinnt die Strategie nie
(0 von 108 Kombinationen), für andere Dokumenttypen aber schon — und `filter_text`
ist ein allgemeiner Einstiegspunkt. Statt sie zu entfernen wird sie nur noch einmal
statt je Retry berechnet (`_sections` kennt keinen `context`): 499 ms → 368 ms,
Output byte-identisch über 54 Vergleiche.
