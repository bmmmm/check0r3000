# config/ — quote profile, query builder inputs, weights & taxonomy

This holds the **reproducible CHECK24 quote setup** plus the TUI's curation and
weighting files. Nothing here touches a model.

## Files

| File | Tracked? | What |
|---|---|---|
| `check24-profile.json` | **no** (gitignored) | Your real quote: `base_url` + the exact CHECK24 `query` string. Contains birthdate + zipcode (PII). |
| `check24-profile.example.json` | yes | Placeholder twin with fake birthdate/zipcode. Copy it to `check24-profile.json` and edit. |
| `check24-providers.json` | yes | `provider_filter` ID → insurer name. Public; used to pin one insurer by id. |
| `favorites.json` | yes | Curated shortlist for the TUI Favorites board. PII-free by contract: only insurer/product/SB-band/tag/stem (+ `recommended`/`reference` flags). No prices — those are read from the gitignored snapshot at render time. |
| `favorite-notes.json` | **no** (gitignored) | Free-text notes typed via `[N]` — personal, stem-keyed sidecar, merged over the favorites at render time. |
| `coverage_taxonomy.json` | yes | Canonical benefit/exclusion categories for the Vergleich matrices — maps free-text wording across insurers onto one row (no model call). |
| `magic-weights.json` | yes | Dimension weights + `pool_k` for the Magic Find score. Any subset overrides the code defaults in `magic.MagicWeights`. |
| `needs-weights.json` | yes | Personal per-module relevance (0–3 scale, `[W]` editor) for the `[P]` Bedarf mode. Neutral 1.0 placeholder = identical to the objective ranking; no PII. |

## Building a result URL — `scripts/check24_query.py`

The profile stores your query **verbatim**; the builder only overrides the levers you
ask for, so every other default you picked on CHECK24 is reproduced exactly.

```sh
uv run scripts/check24_query.py --show           # decode the profile's key levers
uv run scripts/check24_query.py                  # your saved URL, unchanged
uv run scripts/check24_query.py --all-insurers   # drop the ARAG pin -> every insurer
uv run scripts/check24_query.py --provider 11    # pin one insurer by id
uv run scripts/check24_query.py --position 4 --costsharing 1000
```

`--all-insurers` removes `provider_filter` and `tariff_package` (the two params that pin
a single insurer / package). Everything else — couple, age, Bonn, the four modules,
BahnCard + green-energy discount, Stiftung-Warentest filter — stays as you set it.

## Provider map — do we need to fetch the list?

Yes, once: `provider_filter` is a numeric id (`11` = ARAG), and the id→name table is not
in the URL. It is harvested from the comparison page's provider filter into
`check24-providers.json`. After that, `--provider <id>` works offline. Re-run the harvest
(see `scripts/check24_scrape.js` / the browser step) when CHECK24 adds insurers.

## From URL to documents

A result page lists tariffs in the DOM (no JSON API — the query *is* the payload). Paste
`scripts/check24_scrape.js` into DevTools to scrape rows and harvest each tariff's
source-PDF URLs. Those URLs are persisted (not the PDFs) in
`data/sources/check24-documents.json`; `scripts/fetch_docs.py` downloads selected ones
into `data/inbox/` on demand, where `intake.py` classifies them.

```sh
uv run scripts/fetch_docs.py --check              # probe every saved URL is reachable (no download)
uv run scripts/fetch_docs.py <stem>               # dry-run: show what one tariff would fetch
uv run scripts/fetch_docs.py <stem> --apply       # actually download (third-party copyright)
```

`--check` sends only HEAD / 0-byte-range requests, so it verifies "could we download
this?" without pulling any copyrighted PDF.
