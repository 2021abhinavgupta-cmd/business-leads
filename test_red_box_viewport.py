"""
Regression tests for the red-box viewport-bounds bug fixed 2026-08-07.

_run_axe_audit's box-picking loop used to accept any bounding_box() result
without checking it was actually inside the captured screenshot. Two real,
live-reproduced failure modes: an off-screen element (bounding_box() reports
full-document coordinates, not viewport-clipped ones) produced an invisible
box, and a near-full-viewport wrapper produced a box that highlighted almost
the entire screenshot instead of something specific. Both are reproduced
here with real Playwright + real axe-core against local HTML (no network),
same pattern as test_real_web_vitals.py but exercising the real browser
instead of a faked one, since the bug lived in real bounding_box() geometry.
"""

import pytest
from playwright.async_api import async_playwright

from analyzer.visuals import _run_axe_audit

VIEWPORT = {"width": 1280, "height": 800}


async def _run_against_html(html: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport=VIEWPORT)
        await page.set_content(html)
        try:
            violations, visual_flaw = await _run_axe_audit(page)
        finally:
            await browser.close()
        return violations, visual_flaw


async def test_off_screen_violation_is_rejected_not_drawn_invisibly():
    # A button (empty, so axe's button-name rule fires) sitting 5000px down
    # the page — bounding_box() will happily return it, but it's nowhere
    # near the 800px-tall viewport screenshot.
    html = """
    <html><body>
        <div style="height: 5000px;"></div>
        <button></button>
    </body></html>
    """
    violations, visual_flaw = await _run_against_html(html)
    assert any(v["id"] == "button-name" for v in violations)
    assert visual_flaw is None


async def test_near_full_viewport_wrapper_is_rejected():
    # The only violating element is a wrapper that fills virtually the
    # whole viewport — technically in-bounds, but not a "specific" flaw.
    html = """
    <html><body>
        <button style="width: 1270px; height: 790px;"></button>
    </body></html>
    """
    violations, visual_flaw = await _run_against_html(html)
    assert any(v["id"] == "button-name" for v in violations)
    assert visual_flaw is None


async def test_small_in_viewport_violation_is_still_drawn():
    # Sanity check: the fix must not have made every box rejected — a
    # normal small in-bounds element should still be picked up.
    html = """
    <html><body>
        <button style="width: 40px; height: 20px;"></button>
    </body></html>
    """
    violations, visual_flaw = await _run_against_html(html)
    assert any(v["id"] == "button-name" for v in violations)
    assert visual_flaw is not None
    x0, y0, x1, y1 = visual_flaw["box"]
    assert 0 <= x0 < x1 <= VIEWPORT["width"]
    assert 0 <= y0 < y1 <= VIEWPORT["height"]
