# data/hausrat/offers/ — structured price / Summe overlay (no LLM)

The premium (`beitrag`), the chosen **Versicherungssumme** and the chosen
`Selbstbeteiligung` are **not** in the AVB/PIB PDFs — they are contract choices. The
extraction step correctly leaves them `null` (and `benchmarks/hausrat/golden.json`
pins the premium `null` to prove the model never invents one). Those facts come from a
**check24 result row or a personal Angebot** — a structured source you fill in by hand.

`scripts/overlay.py` reads one file per tariff here, validates it against
`schema/hausrat/offer.schema.json`, and merges it onto the pure record
`out/hausrat/tariffs/<key>.json` into `out/hausrat/enriched/<key>.json`. **No model is
involved** — every value is copied verbatim from the file you control.

## Format

One file per tariff, named exactly like the tariff stem (the
`out/hausrat/tariffs/<key>.json` filename without `.json`), e.g.
`ammerlaender__exclusiv.json`:

```json
{
  "quelle": "check24-Ergebnisliste 2026-08-31",
  "doctype": "check24",
  "beitrag": { "monatlich_eur": 5.90 },
  "coverage": { "versicherungssumme": "36.400 EUR", "selbstbeteiligung": "150 EUR" }
}
```

- `quelle` (required) — where the numbers came from; overlay copies it into `beitrag.quelle`.
- `beitrag` — give `monatlich_eur` and/or `jaehrlich_eur`. If only the annual figure is
  given, overlay fills the monthly one as `jaehrlich / 12`.
- `coverage.versicherungssumme` — **the field that matters most in Hausrat.** It follows
  from the living space (CHECK24 rechnet rund 650 €/m²) and drives the premium, so a
  record without it cannot be compared meaningfully against another quote. Where the AVB
  states a sum of its own, overlay supersedes it and keeps the original in
  `_overlay_original`.
- `coverage.selbstbeteiligung` — the chosen deductible.
- `modules.<m>.level` — `Basis | Komfort | Premium`, keys per
  `schema/hausrat/tariff.schema.json` (`feuer`, `einbruchdiebstahl`, `wasserschaeden`,
  `naturgefahren`, `tiere`, `fahrzeuganprall`, `diebstahl_erweitert`, `internet`,
  `wohnungsschutzbrief`, `glasschutz`, `fahrradschutz`). **Usually you leave this out:**
  no Hausrat record currently carries a graded level — the tariffs differ by product,
  not by per-module tier. overlay **refuses** a level on a module the record does not
  mark `included`.

Run `uv run scripts/overlay.py` (or the full `./pipeline.sh`) with
`CHECK0R_VERTICAL=hausrat`. `--check` re-validates existing enriched records against
their pure twins without re-merging.

## Where the numbers come from

Hausrat is a **panel-flow** vertical: the result list is virtualized and the documents
live in the Tarifdetails panel (see `harvest` in `config/verticals/hausrat/vertical.json`).
The prices are already in the snapshot — `data/hausrat/snapshots/<date>.json` carries one
row per tariff including `monatlich_eur`, scraped with the quote profile in
`config/verticals/hausrat/check24-profile.json`. The fastest honest way to fill a file
here is to copy the premium of the matching row and name that snapshot in `quelle`.

Mind the profile: a premium is only comparable to another quote for the **same** living
space, Versicherungssumme, building type and Elementar choice. The snapshot's `profile`
label (shown in the TUI header) records which profile produced it.

## Privacy

Real offer files carry **your personal premium** — they are **gitignored**. Only this
`README.md` and `_example.json` are tracked. The enriched output `out/hausrat/enriched/`
is gitignored too. **Never** put a real premium into `_example.json` (it is committed);
its values are deliberately fake placeholders.
