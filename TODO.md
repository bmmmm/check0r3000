# TODO — Effizienz-Plan

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

## Maßnahmen, in Bearbeitungsreihenfolge

### 1. Preis-Scan ohne Browser
Größter Hebel, kleinster Aufwand. Neues `scripts/fetch_prices.py`: HTTP-GET +
Parser über das server-gerenderte HTML, schreibt denselben Snapshot wie
`fetch_ratings.py`. Läuft **in-sandbox** — kein Bypass, kein Chromium, ~3 s
statt ~20 s plus Bypass-Ritual, damit auch cron-/CI-tauglich.

Bewertungen aus dem letzten vollen Snapshot übernehmen und mit
`bewertung_stand: <datum>` markieren — **nicht** auf `null` setzen, sonst kippt
das Magic-Ranking (Gewicht 0,05) bei jedem Schnell-Scan.
`fetch_ratings.py` bleibt als Voll-Scan bestehen, läuft aber nur noch monatlich
oder auf Ansage.

### 2. Plausibilitäts-Guard für den Cross-Stem-Cache
Der Reuse in `extract.py` prüft nur Hash-Gleichheit. Er hat deshalb den
degenerierten Record `arag__premium-familienrecht-2026` (`insurer='arag'`,
`tariff='premium-familienrecht-2026'` — Stem-Fragmente statt Extraktionswerten)
auf einen zweiten Stem kopiert; das wurde zurückgerollt.
Guard: Record nicht als Twin-Quelle verwenden, wenn `insurer`/`tariff` dem Slug
entsprechen. `_curated` wird bereits geschützt, Degeneration bisher nicht.

### 3. Record-Aktualitäts-Invariante
Das Attributions-Gate in `regression.py` prüft das **Manifest**, nicht die
Records. Genau deshalb meldet es „alle 26 korrekt attribuiert", während zwei
Records inhaltlich veraltet sind. Invariante ergänzen: `record.sources` ==
Dokumente des aktuellen Manifest-Eintrags.

### 4. Snapshot nur bei Änderung schreiben
Fünf Snapshots à ~101 KiB, davon zwei mit null Änderungen. Hash-Vergleich vor
dem Schreiben spart Platz und macht `price_history` aussagekräftiger — heute
steht dort Rauschen aus identischen Ständen.

### 5. ARAG-Filter schärfen
Löst den aktuell blockierten Extract. Zwei Ansätze in dieser Reihenfolge:
(a) klären, warum `_filter` bei ARAG nur 45 % trimmt — vermutlich verteilen sich
die Anker über das ganze Dokument, sodass die Kontextfenster verschmelzen;
(b) harte Obergrenze pro Payload mit Priorisierung nach Ankerdichte.

Achtung: `FILTER_VERSION` hochziehen invalidiert den Extract-Cache. Nur
**zusammen mit einem geplanten Re-Extract** anfassen, sonst kostet es alle 26
Tarife. Deshalb zuletzt.

## Bewusst nicht angefasst

**`fetch_docs`-Parallelität.** 91 Dokumente laufen bereits mit `--jobs 6`. Mehr
Parallelität verschärft nur die Truncation — mehrfach belegt am 2026-08-03, als
5 von 6 Dokumenten verloren gingen und mehrere zeitlich gestreckte Läufe nötig
waren. Hier ist Geduld die Optimierung, nicht Durchsatz.

## Offene Altlasten

- `arag__komfort-2026` und `arag__premium-flex-familienrecht-2026` haben
  korrigierte Manifest-Einträge und korrekte lokale PDFs (verifiziert: ARAG ARB
  2026), aber **noch die alten Records**. Die Neu-Extraktion scheitert am
  Payload-Limit (→ Maßnahme 5). Heilt sich beim nächsten erfolgreichen
  Extract-Lauf selbst, da der `_input_hash` nicht mehr passt (Cache-Miss).
- `arag__premium-familienrecht-2026` ist degeneriert (siehe Maßnahme 2) und
  braucht eine echte Neu-Extraktion, keinen Twin-Reuse.
