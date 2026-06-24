# data/offers/ — structured price / Stufe overlay (no LLM)

The premium (`beitrag`), the chosen service **level** per module, and the chosen
`Selbstbeteiligung` are **not** in the AVB/PIB PDFs. The extraction step correctly
leaves them `null` (and `benchmarks/golden.json` pins them `null` to prove the model
never invents them). Those facts come from a **check24 result row, a personal
Angebot, or a Leistungsübersicht** — a structured source you fill in by hand.

`scripts/overlay.py` reads one file per tariff here, validates it against
`schema/offer.schema.json`, and merges it onto the pure record
`out/tariffs/<key>.json` into `out/enriched/<key>.json`. **No model is involved** —
every value is copied verbatim from the file you control.

## Format

One file per tariff, named exactly like the tariff stem (the `out/tariffs/<key>.json`
filename without `.json`), e.g. `arag__premium-2026.json`:

```json
{
  "quelle": "check24-Ergebnisliste 2026-06-23",
  "doctype": "angebot",
  "beitrag": { "monatlich_eur": 12.40, "jaehrlich_eur": 148.80 },
  "modules": { "privat": { "level": "Premium" }, "verkehr": { "level": "Komfort" } },
  "coverage": { "selbstbeteiligung": "150 EUR" }
}
```

- `quelle` (required) — where the numbers came from; overlay copies it into `beitrag.quelle`.
- `beitrag` — give `monatlich_eur` (check24 default) and/or `jaehrlich_eur`. If only the
  annual figure is given, overlay fills the monthly one as `jaehrlich / 12`.
- `modules.<m>.level` — `Basis | Komfort | Premium`. overlay **refuses** a level on a
  module the tariff record does not mark `included` (the Angebot would disagree with the
  AVB — resolve it first). Omit a module to leave its level unset.
- `coverage.selbstbeteiligung` — the chosen deductible.

Run `uv run scripts/overlay.py` (or the full `./pipeline.sh`). `--check` re-validates
existing enriched records against their pure twins without re-merging.

## Privacy

Real offer files carry **your personal premium** — they are **gitignored**. Only this
`README.md` and `_example.json` are tracked. The enriched output `out/enriched/` is
gitignored too. **Never** put a real premium into `_example.json` (it is committed);
its values are deliberately fake placeholders.
