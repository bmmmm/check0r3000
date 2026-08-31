# data/privathaftpflicht/offers/ — structured price / Summe overlay (no LLM)

The premium (`beitrag`), the chosen **Deckungssumme** and the chosen
`Selbstbeteiligung` are **not** in the AVB/PIB PDFs — they are contract choices. The
extraction step correctly leaves them `null` (and
`benchmarks/privathaftpflicht/golden.json` pins the premium `null` to prove the model
never invents one). Those facts come from a **check24 result row or a personal
Angebot** — a structured source you fill in by hand.

`scripts/overlay.py` reads one file per tariff here, validates it against
`schema/privathaftpflicht/offer.schema.json`, and merges it onto the pure record
`out/privathaftpflicht/tariffs/<key>.json` into
`out/privathaftpflicht/enriched/<key>.json`. **No model is involved** — every value is
copied verbatim from the file you control.

## Format

One file per tariff, named exactly like the tariff stem (the
`out/privathaftpflicht/tariffs/<key>.json` filename without `.json`), e.g.
`vhv__exklusiv.json`:

```json
{
  "quelle": "check24-Ergebnisliste 2026-08-28",
  "doctype": "check24",
  "beitrag": { "monatlich_eur": 2.15 },
  "coverage": { "versicherungssumme": "5 Mio EUR", "selbstbeteiligung": null }
}
```

- `quelle` (required) — where the numbers came from; overlay copies it into `beitrag.quelle`.
- `beitrag` — give `monatlich_eur` and/or `jaehrlich_eur`. If only the annual figure is
  given, overlay fills the monthly one as `jaehrlich / 12`.
- `coverage.versicherungssumme` — **the Deckungssumme you actually chose.** PHV tariffs
  are sold at 5 / 10 / 20 / 50 Mio; the AVB usually names the tariff's own maximum, so
  overlay supersedes it with your pick and keeps the AVB value in `_overlay_original`.
  Two premiums are only comparable at the same Deckungssumme.
- `coverage.selbstbeteiligung` — the chosen deductible (`null` when the tariff has none).
- `modules.<m>.level` — `Basis | Komfort | Premium`, keys per
  `schema/privathaftpflicht/tariff.schema.json` (`privat`, `haushalt`, `ehrenamt`,
  `immobilien`, `mietsache`, `tiere`, `verkehr`, `daten`, `ausland`, `umwelt`).
  **Usually you leave this out:** no PHV record currently carries a graded level — the
  tariffs differ by product, not by per-module tier. overlay **refuses** a level on a
  module the record does not mark `included`.

Run `uv run scripts/overlay.py` (or the full `./pipeline.sh`) with
`CHECK0R_VERTICAL=privathaftpflicht`. `--check` re-validates existing enriched records
against their pure twins without re-merging.

## Where the numbers come from

PHV is a **panel-flow** vertical: the result list is virtualized and the documents live
in the Tarifdetails panel (see `harvest` in
`config/verticals/privathaftpflicht/vertical.json`). The prices are already in the
snapshot — `data/privathaftpflicht/snapshots/<date>.json` carries one row per tariff
including `monatlich_eur`, scraped with the quote profile in
`config/verticals/privathaftpflicht/check24-profile.json`. The fastest honest way to
fill a file here is to copy the premium of the matching row and name that snapshot in
`quelle`.

Mind the profile: a premium is only comparable for the **same** Deckungssumme, number of
coinsured people and deductible. The snapshot's `profile` label (shown in the TUI header)
records which profile produced it.

## Privacy

Real offer files carry **your personal premium** — they are **gitignored**. Only this
`README.md` and `_example.json` are tracked. The enriched output
`out/privathaftpflicht/enriched/` is gitignored too. **Never** put a real premium into
`_example.json` (it is committed); its values are deliberately fake placeholders.
