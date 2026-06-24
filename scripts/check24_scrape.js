// check24_scrape.js — extract Rechtsschutz tariff rows from a CHECK24 result page.
//
// CHECK24 has NO JSON results API: the comparison is rendered server-side from the
// query string, so every tariff lives in the page DOM. The scrape reads that DOM only.
// (check24Docs() additionally clicks CHECK24's own "Tarifdetails" buttons, which trigger
// CHECK24's same-origin lazy-load — still no third-party calls and nothing is sent out.)
//
// Usage:
//   1. Open a result page, e.g.
//      https://rechtsschutz.check24.de/rsv/vergleichsergebnis/?...&module_priv=yes&...
//      (drop provider_filter / tariff_package to see ALL insurers, keep them to pin one.)
//   2. Open DevTools (F12) → Console.
//   3. Paste this whole file, press Enter.
//
// It prints a console.table of all rows and a JSON array (also copied to the
// clipboard in DevTools via copy()). Then:
//   window.check24Offer(<position>)        -> an offer skeleton for one row.
//   await window.check24Docs(<pos>, ...)   -> the source-document (AVB/PIB/…) URLs
//                                             from each tariff's "Tarifdetails" panel.
// See data/offers/README.md for the mapping rules (which row → which tariff stem,
// the per-module `level` caveat, and that document PDFs are third-party/gitignored).

(() => {
  const norm = (s) => (s || "").replace(/ /g, " "); // collapse NBSP (CHECK24 uses it around prices)
  const eur = (s) => (s == null ? null : Number(s.replace(/\./g, "").replace(",", ".")));

  // CHECK24 filestore "kind" path segment -> our schema sources.doctype.
  const KIND_TO_DOCTYPE = {
    tariff_terms: "avb",
    tariff_terms_extra: "avb", // Besondere Versicherungsbedingungen
    tariff_infos: "produktinfoblatt",
    tariff_concatenated_additional_documents: "weitere_unterlagen",
  };

  // A result card is the SMALLEST element that mentions Selbstbeteiligung, a monthly
  // price and a Tarifnote/Tarifdetails block — i.e. no child of it also qualifies.
  const qualifies = (e) => {
    const t = norm(e.innerText);
    return /Selbstbeteiligung/.test(t) && /monatlich/.test(t)
      && /Tarifnote|Tarifdetails/.test(t) && t.length < 800;
  };
  const cards = [...document.querySelectorAll("div,li,article,section")]
    .filter((e) => qualifies(e) && ![...e.children].some(qualifies));

  const seen = new Set();
  const rows = [];
  const cardOf = new Map(); // row object -> its card element (for expanding Tarifdetails)
  for (const c of cards) {
    const t = norm(c.innerText);
    const priceStr = (t.match(/monatlich\s*([\d.]+,\d{2})\s*€/) || [])[1];
    if (!priceStr) continue;

    const lines = t.split("\n").map((s) => s.trim()).filter(Boolean);
    const position = parseInt(lines[0], 10) || null;
    const img = c.querySelector("img[alt]");
    const insurer = img ? img.alt.trim() : null;
    const product = lines[1] || null;

    const key = `${position}|${insurer}|${product}|${priceStr}`;
    if (seen.has(key)) continue;
    seen.add(key);

    const grab = (re) => { const m = t.match(re); return m ? m[1].trim() : null; };
    const row = {
      position,
      insurer,
      product,
      tarifnote: grab(/([\d],[\d])\s*\n?\s*Tarifnote/) || grab(/Tarifnote[:\s]*([\d],[\d])/),
      monatlich_eur: eur(priceStr),
      selbstbeteiligung: grab(/Selbstbeteiligung[:\s]*([^\n]+)/),
      deckungssumme: grab(/Deckungssumme[:\s]*([^\n]+)/),
      wartezeit: grab(/Wartezeit[:\s]*([^\n]+)/),
    };
    rows.push(row);
    cardOf.set(row, c);
  }
  rows.sort((a, b) => (a.position || 0) - (b.position || 0));

  // Sanity check: a silent under-count is the main failure mode if CHECK24 shifts its
  // markup (relabelled fields, a card pushed past the 800-char ceiling, a price moved
  // out of the innermost element). Compare against an independent signal — the count
  // of "monatlich … €" price strings on the page — and warn loudly on drift.
  const priceSignals = (norm(document.body.innerText).match(/monatlich\s*[\d.]+,\d{2}\s*€/g) || []).length;
  if (priceSignals && rows.length < priceSignals) {
    console.warn(`check24_scrape: scraped ${rows.length} rows but found ${priceSignals} price `
      + `labels — markup may have shifted, some tariffs were dropped silently.`);
  }

  // Build an offer skeleton for one row, ready to drop into data/offers/<key>.json.
  // NOTE: `modules` is left empty on purpose — only YOU know which check24 product
  // tier maps to the schema's Basis/Komfort/Premium `level` (and many insurers are
  // not tier-graded at all, in which case leave it unset). See data/offers/README.md.
  const offerFor = (position) => {
    const r = rows.find((x) => x.position === position);
    if (!r) { console.warn(`no row at position ${position}`); return null; }
    const today = new Date().toISOString().slice(0, 10);
    const offer = {
      quelle: `check24-Ergebnisliste ${today} — ${r.insurer} ${r.product}, Position ${r.position}`,
      doctype: "check24",
      beitrag: { monatlich_eur: r.monatlich_eur },
      modules: {},
      coverage: { selbstbeteiligung: r.selbstbeteiligung },
    };
    const json = JSON.stringify(offer, null, 2);
    console.log(json);
    if (typeof copy === "function") copy(json);
    return offer;
  };

  // Harvest the source-document URLs CHECK24 reveals once a tariff's "Tarifdetails"
  // panel is expanded — one filestore hash per tariff, with the AVB / Produktinfoblatt
  // / weitere Unterlagen PDFs under it. Pass the result-list positions you want; with
  // none, it reads whatever panels are already open. This only COLLECTS URLs and
  // downloads nothing — the PDFs are third-party/copyrighted (gitignored in this repo);
  // fetch them yourself if you need them.
  const docsFor = async (...positions) => {
    for (const p of positions) {
      const card = cardOf.get(rows.find((x) => x.position === p));
      if (!card) { console.warn(`check24Docs: no row at position ${p}`); continue; }
      const btn = card.querySelector('[class*="details_button"]');
      if (btn) btn.click();
    }
    if (positions.length) await new Promise((res) => setTimeout(res, 3000)); // lazy-load

    const byHash = {};
    for (const a of document.querySelectorAll('a[href*="/filestore/"]')) {
      const m = a.href.match(/\/filestore\/([^/]+)\/([0-9a-f]{16,})\//);
      if (!m) continue;
      const [, kind, hash] = m;
      const name = decodeURIComponent(a.href.split("?")[0].split("/").pop()).replace(/\.pdf$/, "");
      (byHash[hash] = byHash[hash] || { hash, file: name, docs: [] }).docs.push({
        doctype: KIND_TO_DOCTYPE[kind] || kind,
        kind,
        url: a.href.split("?")[0],
      });
    }
    const manifest = Object.values(byHash);
    const json = JSON.stringify(manifest, null, 2);
    console.log(json);
    if (typeof copy === "function") copy(json);
    return manifest;
  };

  console.table(rows);
  const json = JSON.stringify(rows, null, 2);
  if (typeof copy === "function") copy(json); // DevTools console helper
  window.check24Rows = rows;
  window.check24Offer = offerFor;
  window.check24Docs = docsFor;
  return `check24_scrape: ${rows.length} rows in window.check24Rows (JSON copied to clipboard). `
    + `check24Offer(<pos>) prints an offer skeleton; await check24Docs(<pos>, ...) harvests `
    + `the AVB/PIB document URLs from each tariff's Tarifdetails panel.`;
})();
