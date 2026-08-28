"""Shared headless-scan helpers for CHECK24 result pages.

Stdlib-only leaf (mirrors _vertical.py / _modules.py): Playwright objects are
passed IN — this module never imports playwright, so importing it stays free.
Used by fetch_ratings.py (market scan) and harvest_docs.py (document harvest)
for the flow=panel verticals, whose result lists are VIRTUALIZED: cards mount
and unmount again with real scroll progress, so no single DOM snapshot holds
every row — and mouse.wheel does not trigger the lazy-mount in headless
Chromium at all (measured 2026-08-28: stuck at 3 cards; window.scrollBy works).
"""
from __future__ import annotations

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


async def dismiss_overlays(page) -> None:
    """Best-effort dismissal of the layers a fresh result page may put over the
    list: the cookie consent, then the "OK, Infos erhalten" info layer some
    verticals (phv) show — either can block scroll-driven lazy-mounting."""
    for click in (
        lambda: page.locator(".c24-cookie-consent-functional").click(timeout=3000),
        lambda: page.get_by_text("OK, Infos erhalten").first.click(timeout=4000),
    ):
        try:
            await click()
            await page.wait_for_timeout(1200)
        except Exception:
            pass


async def at_bottom(page) -> bool:
    """Has the window scrolled to (near) the end of the document?"""
    return await page.evaluate(
        "() => window.scrollY + window.innerHeight "
        ">= document.body.scrollHeight - 50")


async def accumulate_rows(page, js: str, *, max_rounds: int = 60,
                          step: int = 2200, settle_ms: int = 1100) -> list[dict]:
    """Walk a virtualized result list downward and ACCUMULATE scraped rows.

    `js` is check24_scrape.js source; it is (re-)evaluated after every scroll
    step and the rows are merged by their markup position (stable across mount
    windows — the DOM index is not). Stops when a round at the bottom adds
    nothing, twice in a row. Returns the rows position-sorted."""
    acc: dict[int, dict] = {}

    async def scrape_round() -> None:
        await page.evaluate(js)
        for r in await page.evaluate("() => window.check24Rows || []"):
            pos = r.get("position")
            if pos is not None and pos not in acc:
                acc[pos] = r

    await scrape_round()
    stable = 0
    for _ in range(max_rounds):
        await page.evaluate(f"() => window.scrollBy(0, {step})")
        await page.wait_for_timeout(settle_ms)
        before = len(acc)
        await scrape_round()
        stable = stable + 1 if (len(acc) == before and await at_bottom(page)) else 0
        if stable >= 2:
            break
    return [acc[p] for p in sorted(acc)]
