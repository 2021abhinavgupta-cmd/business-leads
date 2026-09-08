"""
Real-browser tests for the highlight (a magenta box + a black/white label
tag, since 2026-09-08 — see analyzer/visuals.py's _HIGHLIGHT_COLOR comment
for why it moved off red).

Originally written for the 2026-08-07 viewport-bounds bug: the box-picking
loop accepted any bounding_box() result without checking it was inside the
captured screenshot, so an off-screen element produced an invisible box and a
near-full-viewport wrapper produced one that highlighted the whole image.
Those two cases are still covered here.

Rewritten for the in-browser highlight. The box used to be measured in Python
and painted onto the image bytes with Pillow, which meant the capture and the
measurement happened at different moments — Playwright's own documentation
says bounding-box coordinates are only safe to reuse "assuming the page is
static", and a page with a carousel, a settling layout or a late banner is
not. The outline is now injected into the DOM and rendered by the browser into
the screenshot itself.

That change makes a stronger assertion possible than the old suite could
make: these tests read the actual PIXELS back out of the capture and check the
outline is really there, and really around the element axe-core objected
to. The old tests could only check a coordinate tuple, which is exactly the
thing that could be right while the image was wrong.

Real Playwright + real axe-core against local set_content HTML. No network, no
API key — only a browser, which CI already installs.
"""

from io import BytesIO

import pytest
from PIL import Image
from playwright.async_api import async_playwright

from analyzer.visuals import (
    _MIN_HIGHLIGHT_PX,
    _capture,
    _highlight_element,
    _remove_highlight,
    _run_axe_audit,
)

VIEWPORT = {"width": 1280, "height": 800}


async def _run_against_html(html: str, settle_ms: int = 0):
    """
    Drive the real per-page sequence: audit, highlight, capture.

    Mirrors _audit_current_page's ordering exactly, because the ordering is
    what is under test.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport=VIEWPORT)
        await page.set_content(html)
        if settle_ms:
            await page.wait_for_timeout(settle_ms)
        try:
            violations, candidates = await _run_axe_audit(page)
            highlight = await _highlight_element(page, candidates)
            image = await _capture(page)
            await _remove_highlight(page)
        finally:
            await browser.close()
        return violations, highlight, image


def _marker_pixels(png_bytes):
    """
    Bounding box of the magenta outline in a capture, or None.

    Renamed from _red_pixels 2026-09-08 when the marker color changed from
    red to magenta (see analyzer/visuals.py's _HIGHLIGHT_COLOR comment) — a
    reader had mistaken a real site's own native red icon for this pipeline's
    marker, and red is exactly the color a real site's own UI reaches for
    (error states, alert badges), so it was a poor choice for something that
    must never be confused with real content. The label tag added alongside
    the box is deliberately black/white, not magenta, specifically so it
    contributes no pixels here and this function still measures the box
    alone — see the comment on the label's style in _highlight_element.
    """
    img = Image.open(BytesIO(png_bytes)).convert("RGB")
    xs, ys = [], []
    for x, y in ((x, y) for y in range(0, img.height, 2) for x in range(0, img.width, 2)):
        r, g, b = img.getpixel((x, y))
        if r > 200 and b > 200 and g < 80:
            xs.append(x)
            ys.append(y)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


# ---------------------------------------------------------------------------
# The two originally-reported failure modes
# ---------------------------------------------------------------------------

async def test_off_screen_violation_is_rejected_not_drawn_invisibly():
    # A button (empty, so axe's button-name rule fires) sitting 5000px down
    # the page. It is a perfectly real violation with a perfectly valid box,
    # but it is nowhere near the 800px-tall viewport that gets captured.
    html = """
    <html><body style="margin:0">
        <div style="height: 5000px;"></div>
        <button></button>
    </body></html>
    """
    violations, highlight, image = await _run_against_html(html)
    assert any(v["id"] == "button-name" for v in violations)
    assert highlight is None
    # And nothing was drawn — the old bug produced a box the recipient could
    # not see while the copy still described one.
    assert _marker_pixels(image) is None


async def test_a_wrapper_covering_most_of_the_frame_is_rejected():
    """
    Technically in-bounds, but highlighting it says "the whole page is wrong"
    rather than pointing at anything.

    Sized to fail the AREA guard while staying comfortably inside the
    viewport, so this exercises the area check specifically rather than
    accidentally being caught by the bounds check. The old guard was an AND
    over both dimensions, which this element would have passed.
    """
    html = """
    <html><body style="margin:0">
        <button style="width: 1000px; height: 700px;"></button>
    </body></html>
    """
    violations, highlight, image = await _run_against_html(html)
    assert any(v["id"] == "button-name" for v in violations)
    assert highlight is None
    assert _marker_pixels(image) is None


async def test_a_small_in_viewport_violation_is_still_highlighted():
    """The guards must not have made every candidate unhighlightable."""
    html = """
    <html><body style="margin:0">
        <div style="height: 100px;"></div>
        <button style="width: 40px; height: 20px;"></button>
    </body></html>
    """
    violations, highlight, image = await _run_against_html(html)
    assert any(v["id"] == "button-name" for v in violations)
    assert highlight is not None
    assert highlight["width"] >= _MIN_HIGHLIGHT_PX
    assert highlight["height"] >= _MIN_HIGHLIGHT_PX
    assert 0 <= highlight["x"] <= VIEWPORT["width"]
    assert 0 <= highlight["y"] <= VIEWPORT["height"]


# ---------------------------------------------------------------------------
# The box is in the image, and it is around the right thing
# ---------------------------------------------------------------------------

async def test_the_outline_is_actually_present_in_the_captured_pixels():
    """
    The property the old architecture could not check. A coordinate tuple can
    be perfectly correct while the image has no box in it, or a box somewhere
    else entirely.
    """
    html = """
    <html><body style="margin:0">
        <div style="height: 200px;"></div>
        <button style="width: 120px; height: 60px;"></button>
    </body></html>
    """
    _violations, highlight, image = await _run_against_html(html)
    assert highlight is not None
    assert _marker_pixels(image) is not None


async def test_the_outline_lands_on_the_element_axe_objected_to():
    """
    The box in the picture and the element it claims to be about must be the
    same place. This is the assertion that a stale coordinate would fail.
    """
    html = """
    <html><body style="margin:0">
        <div style="height: 300px;"></div>
        <button style="margin-left: 500px; width: 120px; height: 60px;"></button>
    </body></html>
    """
    _violations, highlight, image = await _run_against_html(html)
    assert highlight is not None
    drawn = _marker_pixels(image)
    assert drawn is not None

    # The outline is inset/outset by a few px and sampled every 2px, so allow
    # a small tolerance rather than demanding exact equality.
    tolerance = 12
    assert abs(drawn[0] - highlight["x"]) < tolerance
    assert abs(drawn[1] - highlight["y"]) < tolerance
    assert abs(drawn[2] - (highlight["x"] + highlight["width"])) < tolerance
    assert abs(drawn[3] - (highlight["y"] + highlight["height"])) < tolerance


async def test_the_outline_follows_an_element_that_moved_after_the_audit_ran():
    """
    The regression test for the bug this restructure exists to remove.

    The element is somewhere else by the time the capture happens — exactly
    what a rotating carousel or a late-settling layout does during the seconds
    axe-core takes to run. Measuring in Python and painting afterwards would
    put the box where the element USED to be, and nothing downstream could
    tell: a misplaced box looks precisely like a correctly placed one. Because
    the rect is now read and the outline appended in the same synchronous
    evaluate(), the box cannot be anywhere but on the element.
    """
    html = """
    <html><body style="margin:0">
        <button id="mover" style="position:absolute; left:0px; top:400px; width:120px; height:60px;"></button>
        <script>
            setTimeout(() => {
                document.getElementById('mover').style.left = '700px';
                document.getElementById('mover').style.top = '120px';
            }, 100);
        </script>
    </body></html>
    """
    _violations, highlight, image = await _run_against_html(html, settle_ms=400)
    assert highlight is not None
    # It really did move before the capture.
    assert highlight["x"] > 600
    assert highlight["y"] < 200

    drawn = _marker_pixels(image)
    assert drawn is not None
    tolerance = 12
    assert abs(drawn[0] - highlight["x"]) < tolerance
    assert abs(drawn[1] - highlight["y"]) < tolerance


async def test_an_invisible_element_is_never_highlighted():
    """
    Present in the layout, not painted. A box around it outlines blank space,
    and the copy would describe a flaw the recipient cannot find.
    """
    html = """
    <html><body style="margin:0">
        <div style="height: 100px;"></div>
        <button style="width: 80px; height: 40px; visibility: hidden;"></button>
    </body></html>
    """
    _violations, highlight, image = await _run_against_html(html)
    assert highlight is None
    assert _marker_pixels(image) is None


async def test_the_highlight_is_gone_after_removal():
    """
    The overlay is a real DOM element. Left in place it would be counted by
    the font-consistency and stretched-image checks that run right after it.
    """
    html = """
    <html><body style="margin:0">
        <div style="height: 100px;"></div>
        <button style="width: 80px; height: 40px;"></button>
    </body></html>
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport=VIEWPORT)
        await page.set_content(html)
        try:
            _violations, candidates = await _run_axe_audit(page)
            assert await _highlight_element(page, candidates) is not None
            await _remove_highlight(page)
            after = await _capture(page)
        finally:
            await browser.close()

    assert _marker_pixels(after) is None


# ---------------------------------------------------------------------------
# Deterministic capture
# ---------------------------------------------------------------------------

async def test_a_mid_flight_entrance_animation_is_captured_at_its_end_state():
    """
    Playwright fast-forwards finite animations to completion under
    animations="disabled". Without it, a capture taken two seconds into a
    ten-second fade shows a washed-out page that no real visitor ever sees —
    and the vision model then has a "design problem" to describe that is
    really an artefact of when we pressed the shutter.

    The manual scroll pass this pipeline already does cannot fix this case:
    the element is in view and its IntersectionObserver has fired, the
    animation is simply still running.
    """
    html = """
    <html><body style="margin:0; background:#ffffff">
        <div id="fade" style="width:1280px; height:800px; background:#ff0000;
                              opacity:0; animation: fade 10s linear forwards;"></div>
        <style>@keyframes fade { from { opacity: 0; } to { opacity: 1; } }</style>
    </body></html>
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport=VIEWPORT)
        await page.set_content(html)
        # Deliberately capture very early in a very slow fade.
        await page.wait_for_timeout(200)
        try:
            image = await _capture(page)
        finally:
            await browser.close()

    # Fully opaque red, not a pale pink blend with the white behind it.
    img = Image.open(BytesIO(image)).convert("RGB")
    r, g, b = img.getpixel((640, 400))
    assert r > 240 and g < 40 and b < 40, f"expected the fade to be finished, got {(r, g, b)}"
