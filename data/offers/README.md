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

## Filling it from CHECK24 (scripts/check24_scrape.js)

CHECK24 has **no JSON results API** — the comparison is rendered server-side from the
query string, so the tariffs live in the page DOM. To pull the numbers instead of
typing them:

1. Open a result page on `rechtsschutz.check24.de/rsv/vergleichsergebnis/`. Drop the
   `provider_filter` / `tariff_package` query params to list **all** insurers; keep
   them to pin one. The other params (`module_*`, `costsharing`, `maritalstatus`,
   `birthdate`, `zipcode`, `discounts`) define the quote profile.
2. DevTools (F12) → Console → paste all of `scripts/check24_scrape.js`.
3. It prints a `console.table` of every row and copies a JSON array to the clipboard.
   `window.check24Offer(<position>)` prints an offer skeleton for one row.

The skeleton leaves `modules` **empty on purpose**: only you know whether the product
maps to a Basis/Komfort/Premium `level` (ARAG is tier-graded; ADVOCARD 360° is not —
leave it unset rather than invent one). Fill `modules.<m>.level` by hand per the rules
above, save as `data/offers/<stem>.json`, and run the overlay.

### Source-document URLs

Expanding a tariff's **Tarifdetails** panel exposes the original PDFs (AVB,
Produktinformationsblatt, Besondere VB, weitere Unterlagen) under one CHECK24
`filestore` hash per tariff. `await window.check24Docs(<position>, ...)` clicks those
panels and returns a manifest that maps CHECK24's document `kind` to our schema
`doctype` (`tariff_terms`→`avb`, `tariff_terms_extra`→`avb` for the Besondere VB,
`tariff_infos`→`produktinfoblatt`,
`tariff_concatenated_additional_documents`→`weitere_unterlagen`; an unknown kind passes
through verbatim and must be mapped by hand). The filenames already
encode insurer + tariff (e.g. `..._ARAG_SE_Premium_(2026).pdf`), so a downloaded PDF
dropped into `data/inbox/` is classified by `intake.py` straight away. **`check24Docs`
only collects URLs** — it downloads nothing. The PDFs are third-party/copyrighted and
stay gitignored; fetch them yourself if you need them.

## Privacy

Real offer files carry **your personal premium** — they are **gitignored**. Only this
`README.md` and `_example.json` are tracked. The enriched output `out/enriched/` is
gitignored too. **Never** put a real premium into `_example.json` (it is committed);
its values are deliberately fake placeholders.
