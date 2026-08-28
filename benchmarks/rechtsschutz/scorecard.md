# Benchmark — Scorecard (Extraktionsqualität)

_Snapshot 2026-06-26, commit `b073212`. Punkte = reine Korrektheit (reproduzierbar). **Faithful 50 / Schema 20 / Halluzinations-frei 15 / Modulabdeckung 15**; Module zählen nur bei schema-validem Output. Latenz/Kosten sind Betriebs-Spalten und fließen NICHT in den Score. DNF = kein erfolgreicher Lauf (z.B. oMLX-RAM-Guard). Modul-Nenner = beste Abdeckung im Batch. Rohdaten je Lauf: `tmp/eval/` (gitignored)._

## advocard / 360-privat-rechtsschutz

| Modell | Input | Faithful (50) | Schema (20) | Halluz-frei (15) | Module (15) | **Score** | ~wall_s | ~Kosten |
|---|---|--:|--:|--:|--:|--:|--:|--:|
| sonnet | avb+pib/filt | 50 | 20 | 15 | 15 | **100** | 99 | $0.512 |
| haiku | avb+pib/filt | 33 | 20 | 10 | 15 | **78** | 121 | $0.165 |

## arag / premium-2026

| Modell | Input | Faithful (50) | Schema (20) | Halluz-frei (15) | Module (15) | **Score** | ~wall_s | ~Kosten |
|---|---|--:|--:|--:|--:|--:|--:|--:|
| omlx:gpt-oss-20b-MXFP4-Q8 | avb+pib+weit | 50 | 20 | 15 | 15 | **100** | 115 | – |
| haiku | avb+pib+weit | 50 | 20 | 15 | 13 | **98** | 48 | $0.262 |
| sonnet | avb+pib+weit | 33 | 20 | 10 | 13 | **76** | 109 | $1.144 |
| opus | avb+pib+weit | 17 | 20 | 5 | 15 | **57** | 92 | $1.085 |
