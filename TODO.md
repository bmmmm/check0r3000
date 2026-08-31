# TODO

## Multi-Vertical — nächste Schritte (Stand 2026-08-28, abgearbeitet am selben Tag)

Grundlage: der gelandete Multi-Vertical-Umbau (`fc25545..696ea15`).
Alle Punkte A–D am 2026-08-28 umgesetzt (Commits `7e6f21e..HEAD`).

### A. E2E & Test-Härtung

- [x] **A1 E2E je neuer Sparte lokal:** beide Sparten via
      `update-all.sh --no-scan` grün (fetch_docs verifizierte alle `/file/`-URLs
      in-sandbox, alles cached, 0.0000 USD, Report); TUI-`[g]`-Livebeweis auf
      Allianz Direct (Provenance-Modell im Confirm, `--only`-Pin, 7/7 grün).
      Dabei gefixt: pipeline.sh/TUI-Funnels liefen hartes `uv run`
      (Sandbox-tot) und `[g]/[F]` extrahierten off-provenance (Cache-Signatur-
      Falle) — alle Funnels leiten die Flags jetzt selbst ab.
- [x] **A2 Selftests sparten-parametrisiert:** Fixtures aus
      `_modules.module_keys()` + Taxonomie der aktiven Sparte;
      `--all-verticals` auf tui_data/magic/coverage_taxonomy via geteiltem
      `_vertical.run_per_vertical()`; ci.yml sweept die Registry.
      Alignment-Cases sind Sparten-DATEN in der Taxonomie-JSON.
- [x] **A3 tui_test:** `vertical_switch_real` (hausrat mit echten Daten,
      Skip ohne Snapshot — Skip-Pfad verifiziert). Suite 28/28.
- [x] **A4 GitHub-CI-Lauf kontrolliert:** Run 33177066302 sweept alle drei
      Sparten auf dem Fresh-Clone (RS 3 Golden + 26/26, hausrat 3/3, phv 3/3,
      Staleness-Skip greift).

### B. Harvest-Generalisierung

- [x] **B1 `harvest_docs.py` generalisiert:** `harvest`-Spec in vertical.json
      (flow=panel: card_sel, Expander, Docs-Tab, doc_link_filter,
      kind_to_doctype); RS-filestore-Pfad regression-bewiesen (JURPRIVAT-
      Re-Harvest = identischer Bundle-Hash). Gelernt: Listen sind
      VIRTUALISIERT (mouse.wheel mountet nichts — scrollBy/Akkumulation nach
      Markup-Position, Card-Jagd je Tarif), PHV-Info-Layer blockt Scroll,
      Expander exact+sichtbar matchen. Live bewiesen: hausrat natura ideal +
      phv Adam Riese L geharvestet+extrahiert.
- [x] **B2 Scan produktiv:** Profile kopiert, `fetch_ratings` spec-aware
      (geteiltes `_scan.py`-Leaf), Snapshots 85 (hausrat) / 79 (phv),
      TUI-`[U]`-E2E auf hausrat 9/9 grün. `fetch_prices` ist für
      Panel-Sparten STRUKTURELL unmöglich (SSR trägt 0 Kacheln) und sagt das
      jetzt statt IncompleteRead-Noise.

### C. Kuration experimental → production

- [x] **C1 Module kuratiert:** hausrat merged `ueberschwemmung_rueckstau` →
      `naturgefahren` (11 Module), phv-10 bestätigt; `included` strict
      boolean in beiden Schemas (Instruction: optionaler Baustein ohne
      Einschluss = false, Marktdefault statt Raten); alle 8 Records
      force-re-extrahiert, 0 nulls.
- [x] **C2 Taxonomien:** 39% → **100% Mapping** in beiden Sparten
      (hausrat +15/+4, phv +15/+19 Kategorien), 24+30 gepinnte
      Alignment-Cases inkl. Precedence-Guards.
- [x] **C3 golden.json:** allianz-direct__direct (11 Invarianten) +
      ao-now__balance-2025 (9) handverifiziert, jede `why` zitiert die
      AVB-Stelle; Gates beidseitig bewiesen (grün + rot auf Fälschung).
- [x] **C4:** `[F]`-Deep-Scan-Batches (pool_k=10) je Sparte,
      external-ratings quellen-belegt kuratiert (Finanztip 25.07.2026,
      F&B/Finanztest als _market_notes), Status-Flip → `production`.

### D. Doku & Hygiene

- [x] **D1 `README.md`:** 14 Pfadklassen auf den Vertical-Namespace gezogen,
      Multi-Vertical-Features (Registry, `[S]`, Scaffolder, Panel-Flow,
      `--all-verticals`) nachgetragen.
- [x] **D2 Worktree `multi-vertical` entfernt** (2026-08-31: clean, kein Prozess
      darin, Branch vollständig in `origin/main` → Worktree + Branch gelöscht;
      Haupt-Checkout per `git pull --ff-only` nachgezogen).

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
