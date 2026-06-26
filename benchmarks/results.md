# Benchmark — Extraktionsmodelle

_Snapshot 2026-06-26, commit `7b35e26`, 3 Lauf/Läufe je Zelle. **Korrektheit** (Schema/Faithful/Module) ist reproduzierbar; **Kosten/Latenz** sind konto- und laufzeitspezifische Momentaufnahmen — nur als Größenordnung lesen. Rohdaten je Lauf: `tmp/eval/` (gitignored)._

| Tarif | Modell | Input | Läufe | Schema | Faithful | Module | Halluz. | ~Kosten | ~wall_s |
|---|---|---|---|---|---|---|---|---|---|
| advocard / 360-privat-rechtsschutz | haiku | avb+pib/filt | 3 | 3/3 | 2/3 | 0-8 | 1 | $0.165 | 121.1 |
| advocard / 360-privat-rechtsschutz | sonnet | avb+pib/filt | 3 | 3/3 | 3/3 | 8 | 0 | $0.512 | 99.1 |
| arag / premium-2026 | haiku | avb+pib+weit | 3 | 3/3 | 3/3 | 7 | 0 | $0.262 | 47.9 |
| arag / premium-2026 | omlx:gpt-oss-20b-MXFP4-Q8 | avb+pib+weit | 3 | 3/3 | 3/3 | 8 | 0 | – | 114.6 |
| arag / premium-2026 | opus | avb+pib+weit | 3 | 3/3 | 1/3 | 8 | 2 | $1.085 | 91.8 |
| arag / premium-2026 | sonnet | avb+pib+weit | 3 | 3/3 | 2/3 | 7 | 1 | $1.144 | 109.2 |

_Faithful = schema-valid **und** jeder behauptete Wert im gefütterten Quelltext belegt **und** Pflichtfelder gesetzt. Halluz. = max. Anzahl nicht belegbarer Wertbehauptungen pro Lauf. Das Grounding ist substring-/ziffern-basiert: ausführliche Paraphrasen ohne wörtliche Zahl können als 'nicht belegt' markiert werden, obwohl korrekt (eher bei reicheren Modell-Antworten) — als robusteres Signal `regression.py` gegen die dokument-gegroundeten Invarianten aus `golden.json` nutzen._
