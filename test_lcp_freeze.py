"""The browser's largest-paint timer must stop counting once real loading is over.
Live bug 2026-10-22: a video swapping in a frame 25+ seconds in registered as the
LCP, and an email told a site owner his page takes 27 seconds (Google measured 1.9s)."""
import io

import pytest
from PIL import Image
from playwright.async_api import async_playwright

from analyzer.visuals import _web_vitals_init_script

_buf = io.BytesIO()
import os as _os
# Noise, not a flat colour: Chrome skips low-detail images when scoring largest paint.
Image.frombytes("RGB", (450, 250), _os.urandom(450 * 250 * 3)).save(_buf, "PNG")
_PNG = _buf.getvalue()   # served over http: Chrome ignores data: URL images for LCP


def _page(late_ms: int) -> str:
    # A small heading at load, a big image shortly after, and a BIGGER one `late_ms` later.
    return f"""<html><body style='margin:0'><h1>Acme</h1>
    <script>
      setTimeout(() => {{ const i = new Image(); i.src = '/a.png'; i.width = 400; i.height = 220;
                          document.body.appendChild(i); }}, 150);
      setTimeout(() => {{ const i = new Image(); i.src = '/b.png'; i.width = 1200; i.height = 700;
                          document.body.appendChild(i); window.__late = performance.now(); }}, {late_ms});
    </script></body></html>"""


async def _lcp(freeze_ms: int, late_ms: int, *, interact: bool = False, wait_ms: int | None = None) -> tuple:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1280, "height": 900})
        await ctx.add_init_script(_web_vitals_init_script(freeze_ms))
        await ctx.route("http://t.test/*.png", lambda r: r.fulfill(
            status=200, content_type="image/png", body=_PNG))
        await ctx.route("http://t.test/", lambda r: r.fulfill(
            status=200, content_type="text/html", body=_page(late_ms)))
        page = await ctx.new_page()
        try:
            await page.goto("http://t.test/", wait_until="load")
            if interact:
                await page.wait_for_timeout(500)
                await page.mouse.wheel(0, 50)
            await page.wait_for_timeout(wait_ms if wait_ms is not None else late_ms + 800)
            lcp = await page.evaluate("() => window.__webVitals.lcp")
            late = await page.evaluate("() => window.__late || null")
            return lcp, late
        finally:
            await browser.close()


async def test_a_paint_long_after_load_is_not_the_lcp():
    lcp, late = await _lcp(freeze_ms=500, late_ms=3000)
    assert lcp is not None and late is not None
    assert lcp < late - 1500        # the late image did not move the number


async def test_without_the_freeze_the_late_paint_wins_which_is_the_bug():
    lcp, late = await _lcp(freeze_ms=60000, late_ms=3000)
    assert lcp is not None and lcp >= late - 200    # proves the test would catch the old behaviour


async def test_a_scroll_also_stops_the_clock():
    lcp, late = await _lcp(freeze_ms=60000, late_ms=3000, interact=True)
    assert lcp is not None and lcp < late - 1500


async def test_a_paint_before_the_freeze_still_counts():
    lcp, _ = await _lcp(freeze_ms=5000, late_ms=900)
    assert lcp is not None and lcp > 100


def test_the_script_is_a_bare_body_not_an_arrow_function():
    js = _web_vitals_init_script(5000).strip()
    assert not js.startswith("(") and "5000" in js and "%d" not in js


