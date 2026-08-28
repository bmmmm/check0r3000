#!/usr/bin/env python3
"""Feasibility probe for a new CHECK24 vertical (T1/T2 of the multi-vertical plan).

Drives a scripted step list through a vertical's funnel with headless Chromium and
dumps evidence after every step (screenshot, URL, interactive elements, text excerpt),
so the step list can be extended iteratively until the result list is reached.

The `scrape` step injects the repo's check24_scrape.js unchanged and records, side by
side: rows it parsed, the independent price-label count, presence of the RS scraper's
structural selectors (.result_box__content / .efeedback-button__count) and the number
of /filestore/ links — exactly the T1/T2 pass criteria.

Must run OUTSIDE the sandbox (chromium launch dies under Seatbelt):
    .venv/bin/python scripts/probe/probe_funnel.py tmp/steps_phv.json ...
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

PROBE_DIR = Path(__file__).resolve().parent
REPO = PROBE_DIR.parent.parent
SCRAPE_JS = REPO / "scripts" / "check24_scrape.js"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DUMP_JS = """() => {
  const vis = (e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const info = (e) => ({
    tag: e.tagName.toLowerCase(),
    id: e.id || null,
    name: e.getAttribute('name'),
    type: e.getAttribute('type'),
    placeholder: e.getAttribute('placeholder'),
    value: e.value != null ? String(e.value).slice(0, 40) : null,
    aria: e.getAttribute('aria-label'),
    cls: (typeof e.className === 'string' ? e.className : '').slice(0, 100) || null,
    text: (e.innerText || e.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 90) || null,
  });
  const els = [...document.querySelectorAll(
      'input,select,button,a,[role=button],[role=radio],[role=option],label')]
    .filter(vis).slice(0, 200).map(info);
  return {
    url: location.href,
    title: document.title,
    ready_selector: !!document.querySelector('.result_box__content'),
    hydration_selector: !!document.querySelector('.efeedback-button__count'),
    filestore_links: document.querySelectorAll('a[href*="/filestore/"]').length,
    text: document.body ? document.body.innerText.slice(0, 5000) : '',
    elements: els,
  };
}"""


def log(msg: str) -> None:
    print(msg, flush=True)


def try_consent(page) -> str:
    """Click a cookie-consent accept button if one is visible (main frame or any
    child frame). Best-effort: a missing banner is not an error."""
    pat = re.compile(r"(alle akzeptieren|akzeptieren|zustimmen|einverstanden|accept all)", re.I)
    for frame in page.frames:
        try:
            btn = frame.get_by_role("button", name=pat).first
            if btn.is_visible(timeout=1500):
                btn.click(timeout=3000)
                return f"clicked in frame {frame.url[:60]}"
        except Exception:
            continue
    return "no consent banner found"


def run_steps(pw, spec: dict) -> None:
    name = spec["name"]
    out = REPO / "tmp" / "vertical-probe" / name
    out.mkdir(parents=True, exist_ok=True)
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page(user_agent=UA, viewport={"width": 1366, "height": 950})
    page.set_default_timeout(20_000)
    results: list[dict] = []

    for i, step in enumerate(spec.get("steps", [])):
        action = step.get("action")
        rec: dict = {"n": i, "action": action, "step": step}
        t0 = time.time()
        try:
            if action == "goto":
                page.goto(step["url"], wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(step.get("settle_ms", 2500))
            elif action == "consent":
                rec["result"] = try_consent(page)
                page.wait_for_timeout(1000)
            elif action == "click":
                if "selector" in step:
                    loc = page.locator(step["selector"])
                elif "text" in step:
                    loc = page.get_by_text(step["text"], exact=step.get("exact", False))
                elif "role" in step:
                    loc = page.get_by_role(step["role"], name=re.compile(step["name"], re.I))
                else:
                    raise ValueError("click needs selector/text/role")
                loc.nth(step.get("nth", 0)).click(timeout=step.get("timeout_ms", 15_000))
                page.wait_for_timeout(step.get("settle_ms", 2500))
            elif action == "fill":
                page.locator(step["selector"]).nth(step.get("nth", 0)).fill(step["value"])
                page.wait_for_timeout(step.get("settle_ms", 500))
            elif action == "select":
                page.locator(step["selector"]).nth(step.get("nth", 0)).select_option(step["value"])
                page.wait_for_timeout(step.get("settle_ms", 500))
            elif action == "press":
                page.keyboard.press(step["key"])
                page.wait_for_timeout(step.get("settle_ms", 1000))
            elif action == "wait":
                page.wait_for_timeout(step.get("ms", 2000))
            elif action == "scroll":
                for _ in range(step.get("times", 5)):
                    page.mouse.wheel(0, step.get("dy", 2500))
                    page.wait_for_timeout(step.get("pause_ms", 1200))
            elif action == "wait_for":
                page.wait_for_selector(step["selector"], timeout=step.get("timeout_ms", 30_000))
            elif action == "eval":
                rec["result"] = page.evaluate(step["js"])
            elif action == "scrape":
                page.evaluate(SCRAPE_JS.read_text(encoding="utf-8"))
                rows = page.evaluate("() => window.check24Rows || []")
                price_signals = page.evaluate(
                    "() => (document.body.innerText.replace(/[\\u00a0\\u202f]/g,' ')"
                    ".match(/monatlich\\s*[\\d.]+,\\d{2}\\s*\\u20ac/g) || []).length")
                jahr_signals = page.evaluate(
                    "() => (document.body.innerText.replace(/[\\u00a0\\u202f]/g,' ')"
                    ".match(/[\\d.]+,\\d{2}\\s*\\u20ac/g) || []).length")
                rec["result"] = {"rows": len(rows), "price_signals_monatlich": price_signals,
                                 "eur_signals_any": jahr_signals}
                (out / f"rows_{i:02d}.json").write_text(
                    json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
            elif action == "docs":
                page.evaluate(SCRAPE_JS.read_text(encoding="utf-8"))
                docs = page.evaluate(
                    "async (ps) => await window.check24Docs(...ps)", step.get("positions", []))
                rec["result"] = {"bundles": len(docs or [])}
                (out / f"docs_{i:02d}.json").write_text(
                    json.dumps(docs, ensure_ascii=False, indent=2), encoding="utf-8")
            elif action == "download_links":
                pat = re.compile(step["pattern"], re.I)
                links = page.evaluate(
                    "() => [...document.querySelectorAll('a[href]')].map(a => "
                    "({href: a.href, text: (a.innerText || '').trim().replace(/\\s+/g, ' ')"
                    ".slice(0, 80)}))")
                hits = [l for l in links if pat.search(l["href"])][: step.get("limit", 3)]
                got = []
                for j, l in enumerate(hits):
                    try:
                        resp = page.request.get(l["href"])
                        body = resp.body()
                        fn = out / f"doc_{i:02d}_{j}.bin"
                        fn.write_bytes(body)
                        got.append({"href": l["href"], "text": l["text"],
                                    "status": resp.status,
                                    "content_type": resp.headers.get("content-type"),
                                    "bytes": len(body), "file": fn.name,
                                    "is_pdf": body[:5] == b"%PDF-"})
                    except Exception as exc:
                        got.append({"href": l["href"], "error": str(exc)[:200]})
                rec["result"] = got
            elif action == "html":
                (out / f"page_{i:02d}.html").write_text(page.content(), encoding="utf-8")
                rec["result"] = "saved"
            else:
                raise ValueError(f"unknown action {action!r}")
            rec["ok"] = True
        except Exception as exc:  # keep going to the dump so the evidence survives
            rec["ok"] = False
            rec["error"] = f"{type(exc).__name__}: {exc}"[:400]
            log(f"  ! step {i} {action}: {rec['error']}")
        rec["ms"] = int((time.time() - t0) * 1000)

        try:
            dump = page.evaluate(DUMP_JS)
        except Exception as exc:
            dump = {"error": str(exc)[:200]}
        rec["url"] = dump.get("url")
        rec["page"] = {k: dump.get(k) for k in
                       ("title", "ready_selector", "hydration_selector", "filestore_links")}
        (out / f"dump_{i:02d}_{action}.json").write_text(
            json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            page.screenshot(path=str(out / f"shot_{i:02d}_{action}.png"),
                            full_page=bool(step.get("full_page")))
        except Exception as exc:
            log(f"  ! screenshot {i}: {exc}")
        results.append(rec)
        log(f"  [{name}] step {i:2d} {action:<8} ok={rec['ok']} url={rec.get('url', '?')[:100]}")
        if not rec["ok"] and step.get("fatal", True) and action in ("goto",):
            break

    (out / "run.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    browser.close()
    log(f"[{name}] done — evidence in {out}")


def main() -> int:
    if len(sys.argv) < 2:
        sys.exit("usage: probe_funnel.py <steps.json> [<steps.json> ...]")
    from playwright.sync_api import sync_playwright
    specs = [json.loads(Path(p).read_text(encoding="utf-8")) for p in sys.argv[1:]]
    with sync_playwright() as pw:
        for spec in specs:
            run_steps(pw, spec)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
