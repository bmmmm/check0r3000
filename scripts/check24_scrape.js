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
  const norm = (s) => (s || "").replace(/[\u00a0\u202f]/g, " "); // collapse NBSP / narrow-NBSP (CHECK24 uses it around prices)
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
    // Customer rating (Kundenbewertung, 0-5 stars) is distinct from the expert
    // Tarifnote (a 1,0-best grade). CHECK24 renders it NOT as text but as five
    // `.star--active` inner spans whose CSS `width` percentages sum to the score
    // (4×100% + 1×10% = 410% -> 4.1). The review count is the German-locale
    // thousands number in `.efeedback-button__count` ("4.713" -> 4713 reviews, NOT
    // a 4.713-star score). Both live in the card subtree, so query `c` directly;
    // a regex over innerText can never see them (the star spans hold no text).
    const starBox = c.querySelector(".rating_stars");
    let bewertung = null;
    if (starBox) {
      const widthSum = [...starBox.querySelectorAll(".star--active")]
        .reduce((sum, s) => sum + (parseFloat(s.style.width) || 0), 0);
      const score = Math.round(widthSum) / 100;
      bewertung = Number.isFinite(score) && score > 0 ? score : null;
    }
    const countEl = c.querySelector(".efeedback-button__count");
    const bewertung_anzahl = countEl
      ? (parseInt(countEl.textContent.replace(/[^\d]/g, ""), 10) || null)
      : null; // strip ALL non-digits: handles "4.713" and a parenthesised "(4.713)"
    // Per-Baustein Wartezeiten aus dem Latenz-Tooltip (Privat/Beruf/Wohnen/Verkehr).
    // The label spans end with a colon ("Privat:") — strip it for clean dict keys.
    const wartezeit_per_modul_raw = {};
    for (const mod of c.querySelectorAll(".tooltip_latency_module")) {
      const label = (mod.querySelector(".tooltip_latency_module__module")?.textContent || "")
        .replace(/:$/, "").trim();
      const wert = (mod.querySelector(".tooltip_latency_module__value")?.textContent || "").trim();
      if (label && wert) wartezeit_per_modul_raw[label] = wert;
    }
    const wartezeit_per_modul = Object.keys(wartezeit_per_modul_raw).length
      ? wartezeit_per_modul_raw : null;
    const row = {
      position,
      insurer,
      product,
      tarifnote: grab(/([\d],[\d])\s*\n?\s*Tarifnote/) || grab(/Tarifnote[:\s]*([\d],[\d])/),
      bewertung,
      bewertung_anzahl,
      monatlich_eur: eur(priceStr),
      selbstbeteiligung: grab(/Selbstbeteiligung[:\s]*([^\n]+)/),
      deckungssumme: grab(/Deckungssumme[:\s]*([^\n]+)/),
      wartezeit: grab(/Wartezeit[:\s]*([^\n]+)/),
      wartezeit_per_modul,
    };
    rows.push(row);
    cardOf.set(row, c);
  }
  rows.sort((a, b) => (a.position || 0) - (b.position || 0));

  // Structured fallback for verticals whose result page uses the BEM `result_tile`
  // component (e.g. Hausrat: prices labeled "pro Monat", no Selbstbeteiligung on the
  // card) instead of the RS-style result_box cards the text heuristic above targets.
  // Runs ONLY when the heuristic pass found nothing, so the RS path stays untouched.
  if (!rows.length) {
    for (const tile of document.querySelectorAll(".result_tile")) {
      const pick = (sel) => {
        const e = tile.querySelector(sel);
        return e ? norm(e.innerText).replace(/\s+/g, " ").trim() : "";
      };
      const position = parseInt(pick(".result_tile__position"), 10) || null;
      const img = tile.querySelector(".result_tile__provider_logo img[alt], img[alt]");
      const insurer = (img && img.alt.trim()) || pick(".result_tile__provider_logo") || null;
      const product = pick(".result_tile__tariff_name") || null;
      const priceText = pick(".result_tile__price");
      const pm = priceText.match(/pro Monat\s*([\d.]+,\d{2})\s*€/)
        || priceText.match(/([\d.]+,\d{2})\s*€/);
      const monatlich_eur = pm ? eur(pm[1]) : null;
      const tarifnote = (pick(".result_tile__grade").match(/([\d],[\d])/) || [])[1] || null;
      const features = pick(".result_tile__features");
      const grabf = (re) => { const m = features.match(re); return m ? m[1].trim() : null; };
      if (!product || monatlich_eur == null) continue;
      const row = {
        position,
        insurer,
        product,
        tarifnote,
        bewertung: null,
        bewertung_anzahl: null,
        monatlich_eur,
        selbstbeteiligung: grabf(/Selbstbeteiligung[:\s]*([^|]+?)(?=\s{2}|$)/),
        deckungssumme: grabf(/(?:Versicherungssumme|Deckungssumme)[:\s]*([\d.,]+\s*(?:Mio\.?\s*)?€)/),
        wartezeit: null,
        wartezeit_per_modul: null,
      };
      rows.push(row);
      cardOf.set(row, tile);
    }
    rows.sort((a, b) => (a.position || 0) - (b.position || 0));
  }

  // Sanity check: a silent under-count is the main failure mode if CHECK24 shifts its
  // markup (relabelled fields, a card pushed past the 800-char ceiling, a price moved
  // out of the innermost element). Compare against an independent signal — the count
  // of "monatlich … €" price strings on the page — and warn loudly on drift.
  const priceSignals = (norm(document.body.innerText).match(/monatlich\s*[\d.]+,\d{2}\s*€/g) || []).length;
  if (priceSignals && rows.length < priceSignals) {
    console.warn(`check24_scrape: scraped ${rows.length} rows but found ${priceSignals} price `
      + `labels — markup may have shifted, some tariffs were dropped silently.`);
  }

  // Customer-rating coverage: if NO card yielded one, the rating likely lives only on
  // the tariff detail page (not the result list) or its markup differs from the
  // heuristics above. Warn so it is not silently lost — bewertung stays null and the
  // TUI shows "—" for it.
  const rated = rows.filter((r) => r.bewertung != null).length;
  if (rows.length && !rated) {
    console.warn(`check24_scrape: parsed 0 customer ratings from ${rows.length} cards — the `
      + `Kundenbewertung may be detail-page-only or use different markup; tune the rating `
      + `regexes in this file. (Expert Tarifnote is unaffected.)`);
  }

  // Build an offer skeleton for one row, ready to drop into data/offers/<key>.json.
  // NOTE: `modules` is left empty on purpose — only YOU know which check24 product
  // tier maps to the schema's Basis/Komfort/Premium `level` (and many insurers are
  // not tier-graded at all, in which case leave it unset). See data/offers/README.md.
  const offerFor = (position) => {
    const r = rows.find((x) => x.position === position);
    if (!r) { console.warn(`no row at position ${position}`); return null; }
    // Local calendar date (toISOString is UTC and can read as yesterday/tomorrow
    // near midnight); this only labels the offer's `quelle` provenance string.
    const now = new Date();
    const today = new Date(now.getTime() - now.getTimezoneOffset() * 60000)
      .toISOString().slice(0, 10);
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
      const rawName = a.href.split("?")[0].split("/").pop();
      // decodeURIComponent throws on a malformed %-escape (e.g. a literal "%" in a
      // filename like "100%_Schutz.pdf"); keep the raw segment rather than letting
      // the URIError abort the entire document harvest.
      let name;
      try { name = decodeURIComponent(rawName); } catch { name = rawName; }
      name = name.replace(/\.pdf$/, "");
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
