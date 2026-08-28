#!/usr/bin/env python3
"""Mini end-to-end harvest for a freshly scaffolded vertical (Phase-D proof tool).

One headless-Chromium pass per vertical: reach the result list, scrape ALL rows via
the repo's check24_scrape.js (RS heuristic + result_tile fallback), then for the top
K distinct tariffs expand the Tarifdetails panel, open the documents tab, classify
the /file/ links by their link text and download them into the vertical's
data/<v>/raw/<insurer>/<tariff>/<doctype>.pdf; finally write the vertical's tracked
doc manifest (data/<v>/sources/check24-documents.json) and a rows dump for
snapshot.py.

SUPERSEDED (2026-08-28): harvest_docs.py now handles the panel flows itself via
the per-vertical "harvest" spec in config/verticals/<v>/vertical.json
(flow=panel) — use that for real harvesting. This probe stays as the recorded
Phase-D evidence path (funnel walk + top-K harvest in one pass).

Run OUTSIDE the sandbox (chromium + non-allowlisted hosts):
    .venv/bin/python scripts/probe/mini_harvest.py hausrat --k 3
    .venv/bin/python scripts/probe/mini_harvest.py privathaftpflicht --k 3
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
import time
from pathlib import Path

PROBE = Path(__file__).resolve().parent
REPO = PROBE.parent.parent
sys.path.insert(0, str(REPO / "scripts"))

SCRAPE_JS = (REPO / "scripts" / "check24_scrape.js").read_text(encoding="utf-8")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

PHV_URL = ("https://privathaftpflicht.check24.de/privathaftpflicht/vergleichsergebnis/"
           "?coinsured=1&birthdate=15.05.1985&zipcode=10115&city=&protection_level=none"
           "&amountinsuredchildren=-1&childrenunder7or10=no&public_service=no"
           "&sortorder=asc&min_insure_sum=5&max_costsharing=0&paymentperiod=year"
           "&insure_date=29.08.2026&grade=4&min_stars=0&costsharing=no&from_ipss=yes")

SPECS = {
    "privathaftpflicht": {
        "card_sel": ".result_box__content",
        # The real expander is the "Tarifdetails" text link INSIDE the card (a click
        # on the surrounding details_button--wrap div toggles nothing); once the
        # panel is open the /file/ links are in the DOM behind the inactive
        # "Anbieter & Dokumente" tab — no tab click needed (proven by probe phv11).
        "expand_text": "Tarifdetails",
        "docs_tab_text": None,
        "doc_link_filter": "/file/",
    },
    "hausrat": {
        "card_sel": ".result_tile",
        "details_btn": ".result_tile__details",
        "docs_tab_text": "Dokumente",
        "doc_link_filter": "/file/",
    },
}

IDENTITY_JS = """(sel) => [...document.querySelectorAll(sel)].map((c, i) => {
  const img = c.querySelector('img[alt]');
  const nameEl = c.querySelector('.result_tile__tariff_name');
  const lines = c.innerText.split('\\n').map(s => s.trim()).filter(Boolean);
  return {
    idx: i,
    insurer: img ? img.alt.trim() : null,
    product: nameEl ? nameEl.innerText.trim() : (lines[1] || null),
  };
})"""

LINKS_JS = """(filter) => [...document.querySelectorAll('a[href]')]
  .filter(a => a.href.includes(filter))
  .map(a => ({href: a.href, text: (a.innerText || '').replace(/\\s+/g, ' ').trim()}))"""


def slugify(s: str) -> str:
    s = (s or "").lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def classify(text: str, url: str, taken: set[str]) -> str | None:
    t = (text + " " + url).lower()
    if "versicherungsbedingungen" in t or "avb" in t:
        return "avb" if "avb" not in taken else "avb_besondere"
    if "produktinformationsblatt" in t or "informationsblatt" in t or "ipid" in t:
        return "produktinfoblatt"
    if "erstinformation" in t:  # CHECK24's own broker info sheet, not a tariff doc
        return None
    return "weitere_unterlagen" if "weitere_unterlagen" not in taken else None


def kind_from_url(url: str, doctype: str) -> str:
    m = re.search(r"/file/tariff/([a-z_]+)/", url)
    return m.group(1) if m else doctype


def reach_results(page, vertical: str) -> None:
    if vertical == "privathaftpflicht":
        page.goto(PHV_URL, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(3500)
        for sel_text in (None, "OK, Infos erhalten"):
            try:
                if sel_text is None:
                    page.locator(".c24-cookie-consent-functional").click(timeout=3000)
                else:
                    page.get_by_text(sel_text).first.click(timeout=4000)
                page.wait_for_timeout(1200)
            except Exception:
                pass
    else:  # hausrat: short funnel with invented, PII-free values
        page.goto("https://hausratversicherungen.check24.de/hausrat/benutzereingaben/"
                  "?squaremeter=80", wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(2500)
        try:
            page.locator(".c24-cookie-consent-functional").click(timeout=3000)
            page.wait_for_timeout(1000)
        except Exception:
            pass
        page.locator("input[name=zipcode]").first.fill("10115")
        page.locator("input[name=birthdate]").first.fill("15.05.1985")
        page.wait_for_timeout(800)
        page.get_by_text("Ich bestätige, die Erstinformationen").click()
        page.wait_for_timeout(600)
        page.locator("button[name=toResult]").click()
        page.wait_for_timeout(9000)
    for _ in range(8):
        page.mouse.wheel(0, 3000)
        page.wait_for_timeout(900)
    page.mouse.wheel(0, -100_000)
    page.wait_for_timeout(1500)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("vertical", choices=sorted(SPECS))
    ap.add_argument("--k", type=int, default=3, help="distinct tariffs to harvest")
    args = ap.parse_args()
    v, spec = args.vertical, SPECS[args.vertical]
    today = datetime.date.today().isoformat()

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(user_agent=UA, viewport={"width": 1366, "height": 950})
        page.set_default_timeout(20_000)
        reach_results(page, v)

        page.evaluate(SCRAPE_JS)
        rows = page.evaluate("() => window.check24Rows || []")
        rows_path = REPO / "tmp" / f"scan_{v}.json"
        rows_path.parent.mkdir(exist_ok=True)
        rows_path.write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                             encoding="utf-8")
        print(f"[{v}] scraped {len(rows)} rows -> {rows_path.name}")
        if len(rows) < 10:
            print(f"[{v}] FAIL: fewer than 10 rows", file=sys.stderr)
            browser.close()
            return 1

        cards = page.evaluate(IDENTITY_JS, spec["card_sel"])
        picked: list[dict] = []
        seen_products: set[tuple] = set()
        for c in cards:
            keyt = ((c.get("insurer") or "").casefold(), (c.get("product") or "").casefold())
            if not c.get("product") or keyt in seen_products:
                continue
            seen_products.add(keyt)
            picked.append(c)
            if len(picked) >= args.k:
                break
        print(f"[{v}] harvesting {len(picked)} tariffs: "
              + ", ".join(f"{c['insurer']}/{c['product']}" for c in picked))

        seen_hrefs: set[str] = set(
            l["href"] for l in page.evaluate(LINKS_JS, spec["doc_link_filter"]))
        manifest_tariffs = []
        for c in picked:
            card = page.locator(spec["card_sel"]).nth(c["idx"])
            try:
                card.scroll_into_view_if_needed(timeout=8000)
                if spec.get("expand_text"):
                    expander = card.get_by_text(spec["expand_text"]).first
                else:
                    expander = card.locator(spec["details_btn"]).first
                expander.click(timeout=8000)
                page.wait_for_timeout(6000)
                if spec.get("docs_tab_text"):
                    # Real Playwright click on THIS card's docs tab (a JS
                    # el.click() does not fire the Vue tab handler), scoped to
                    # the card's parent, widening to the grandparent as fallback.
                    try:
                        card.locator("xpath=..").get_by_text(
                            spec["docs_tab_text"]).last.click(timeout=6000)
                    except Exception:
                        card.locator("xpath=../..").get_by_text(
                            spec["docs_tab_text"]).last.click(timeout=6000)
                    page.wait_for_timeout(5000)
            except Exception as exc:
                print(f"  ! {c['product']}: panel/tab failed: {exc}", file=sys.stderr)
                continue
            links = [l for l in page.evaluate(LINKS_JS, spec["doc_link_filter"])
                     if l["href"] not in seen_hrefs]
            ins_slug, prod_slug = slugify(c["insurer"] or "unknown"), slugify(c["product"])
            raw_dir = REPO / "data" / v / "raw" / ins_slug / prod_slug
            docs = []
            taken: set[str] = set()
            for l in links:
                seen_hrefs.add(l["href"])
                doctype = classify(l["text"], l["href"], taken)
                if doctype is None or doctype in taken:
                    continue
                taken.add(doctype)
                resp = page.request.get(l["href"])
                body = resp.body()
                if resp.status != 200 or body[:5] != b"%PDF-":
                    print(f"  ! {c['product']}: {doctype} not a PDF "
                          f"(status {resp.status})", file=sys.stderr)
                    continue
                raw_dir.mkdir(parents=True, exist_ok=True)
                (raw_dir / f"{doctype}.pdf").write_bytes(body)
                docs.append({
                    "doctype": doctype,
                    "kind": kind_from_url(l["href"], doctype),
                    "file": l["href"].split("?")[0].rsplit("/", 1)[-1].removesuffix(".pdf"),
                    "url": l["href"].split("?")[0],
                })
                print(f"  ✓ {c['product']}: {doctype} ({len(body)} B)")
            if docs:
                manifest_tariffs.append({
                    "stem": f"{ins_slug}__{prod_slug}",
                    "insurer": c["insurer"],
                    "tariff": c["product"],
                    "position": c["idx"] + 1,
                    "harvested": today,
                    "docs": docs,
                })
            # collapse the panel again so the next card's new links stay attributable
            try:
                if spec.get("expand_text"):
                    card.get_by_text(spec["expand_text"]).first.click(timeout=5000)
                else:
                    card.locator(spec["details_btn"]).first.click(timeout=5000)
                page.wait_for_timeout(1000)
            except Exception:
                pass
        browser.close()

    if not manifest_tariffs:
        print(f"[{v}] FAIL: no documents harvested", file=sys.stderr)
        return 1

    host = {"privathaftpflicht": "https://privathaftpflicht.check24.de",
            "hausrat": "https://hausratversicherungen.check24.de"}[v]
    manifest_path = REPO / "data" / v / "sources" / "check24-documents.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "quelle": f"check24 {v} vergleichsergebnis — mini_harvest.py (probe profile, "
                  "invented personal values)",
        "host": host,
        "note": "Source-PDF URLs only (PDFs are third-party/copyright -> the vertical's "
                "raw/ is gitignored). fetch_docs.py downloads on demand.",
        "tariffs": manifest_tariffs,
    }
    tmp = manifest_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(manifest_path)
    print(f"[{v}] manifest: {len(manifest_tariffs)} tariffs -> "
          f"{manifest_path.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
