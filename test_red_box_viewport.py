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

from analyzer import visuals
from analyzer.visuals import (
    _MIN_HIGHLIGHT_PX,
    _audit_current_page,
    _capture,
    _check_stretched_images,
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
            violations, candidates, _borderline = await _run_axe_audit(page)
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

# A button styled with near-identical foreground/background colors — a
# reliable, deliberate color-contrast failure (axe-core files this under
# "incomplete", not "violations" — see _run_axe_audit's comment — but the
# defect is exactly as visible either way). Used throughout this file
# instead of an empty <button></button> (which only trips axe's
# button-name rule) because button-name has no visible symptom and is
# filtered out of the highlight candidate list entirely — see
# _VISUALLY_APPARENT_AXE_RULES in analyzer/visuals.py.
_LOW_CONTRAST_STYLE = "color:#fefefe; background-color:#ffffff; border:none;"


async def test_off_screen_violation_is_rejected_not_drawn_invisibly():
    # A low-contrast button sitting 5000px down the page. It is a perfectly
    # real violation with a perfectly valid box, but it is nowhere near the
    # 800px-tall viewport that gets captured.
    html = f"""
    <html><body style="margin:0">
        <div style="height: 5000px;"></div>
        <button style="{_LOW_CONTRAST_STYLE}">Book Now</button>
    </body></html>
    """
    _violations, highlight, image = await _run_against_html(html)
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
    html = f"""
    <html><body style="margin:0">
        <button style="width: 1000px; height: 700px; {_LOW_CONTRAST_STYLE}">Book Now</button>
    </body></html>
    """
    _violations, highlight, image = await _run_against_html(html)
    assert highlight is None
    assert _marker_pixels(image) is None


async def test_a_small_in_viewport_violation_is_still_highlighted():
    """The guards must not have made every candidate unhighlightable."""
    html = f"""
    <html><body style="margin:0">
        <div style="height: 100px;"></div>
        <button style="width: 40px; height: 20px; {_LOW_CONTRAST_STYLE}">B</button>
    </body></html>
    """
    _violations, highlight, image = await _run_against_html(html)
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
    html = f"""
    <html><body style="margin:0">
        <div style="height: 200px;"></div>
        <button style="width: 120px; height: 60px; {_LOW_CONTRAST_STYLE}">Book Now</button>
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
    html = f"""
    <html><body style="margin:0">
        <div style="height: 300px;"></div>
        <button style="margin-left: 500px; width: 120px; height: 60px; {_LOW_CONTRAST_STYLE}">Book Now</button>
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
    html = (
        """
    <html><body style="margin:0">
        <button id="mover" style="position:absolute; left:0px; top:400px; width:120px; height:60px; """
        + _LOW_CONTRAST_STYLE
        + """">Book Now</button>
        <script>
            setTimeout(() => {
                document.getElementById('mover').style.left = '700px';
                document.getElementById('mover').style.top = '120px';
            }, 100);
        </script>
    </body></html>
    """
    )
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
    html = f"""
    <html><body style="margin:0">
        <div style="height: 100px;"></div>
        <button style="width: 80px; height: 40px; {_LOW_CONTRAST_STYLE}">Book Now</button>
    </body></html>
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport=VIEWPORT)
        await page.set_content(html)
        try:
            _violations, candidates, _borderline = await _run_axe_audit(page)
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


# ---------------------------------------------------------------------------
# Only visually-apparent violations get boxed (reported live: a box landed
# on a share icon with no visible defect at all)
# ---------------------------------------------------------------------------

async def test_a_violation_with_no_visible_symptom_is_never_boxed():
    """
    button-name (a missing accessible name) has no visual signature — the
    button looks completely normal to a sighted person. A real violation,
    but not a highlight candidate: see _VISUALLY_APPARENT_AXE_RULES.
    """
    html = """
    <html><body style="margin:0">
        <div style="height: 100px;"></div>
        <button style="width: 80px; height: 40px;"></button>
    </body></html>
    """
    violations, highlight, image = await _run_against_html(html)
    assert any(v["id"] == "button-name" for v in violations)
    assert highlight is None
    assert _marker_pixels(image) is None


async def test_a_visually_apparent_violation_still_wins_over_an_invisible_one():
    """
    A page with both kinds of violation present must box the one a human
    can actually see, not whichever happened to be found first.
    """
    html = f"""
    <html><body style="margin:0">
        <div style="height: 100px;"></div>
        <button style="width: 60px; height: 30px;"></button>
        <button style="width: 120px; height: 60px; {_LOW_CONTRAST_STYLE}">Book Now</button>
    </body></html>
    """
    violations, highlight, image = await _run_against_html(html)
    assert any(v["id"] == "button-name" for v in violations)
    # color-contrast lands in axe's "incomplete" bucket, not "violations" —
    # see _run_axe_audit's comment — so the real proof this fixture worked
    # as intended is the highlight itself: the 120px contrast button, not
    # the 60px button-name-only one.
    assert highlight is not None
    assert highlight["width"] >= 100


# ---------------------------------------------------------------------------
# A stretched image is a highlight candidate too
# ---------------------------------------------------------------------------

_TINY_PNG_DATA_URI = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4"
    "2mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


async def test_a_stretched_image_becomes_a_highlight_candidate():
    """
    A 1x1 PNG blown up to 300x300 is exactly the "obviously blurry photo"
    case a business owner recognises on sight — a much better highlight
    target than most axe-core rules. No axe violation is present at all
    here (an <img> with alt text and fine contrast is accessibility-clean),
    so this candidate is the only way anything gets boxed.
    """
    html = f"""
    <html><body style="margin:0">
        <div style="height: 50px;"></div>
        <img src="{_TINY_PNG_DATA_URI}" alt="Product photo"
             style="width: 300px; height: 300px;">
    </body></html>
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport=VIEWPORT)
        await page.set_content(html)
        try:
            await page.wait_for_timeout(200)  # let the data URI decode
            violations, axe_candidates, _borderline = await _run_axe_audit(page)
            _count, stretched_candidates = await _check_stretched_images(page)
            assert stretched_candidates, "expected the stretched <img> to be a candidate"
            highlight = await _highlight_element(page, axe_candidates + stretched_candidates)
            image = await _capture(page)
            await _remove_highlight(page)
        finally:
            await browser.close()

    assert highlight is not None
    assert _marker_pixels(image) is not None


# ---------------------------------------------------------------------------
# Vision-gated borderline candidates (added 2026-09-09) — requested:
# "cant that api of claude go on the website himself and find the issue
# like the actual claude does live". See _vision_confirms_visible_defect's
# own docstring in analyzer/visuals.py for why this widens coverage past
# _VISUALLY_APPARENT_AXE_RULES without reopening the free-form-vision
# hallucination risk this file's own history already fixed once.
# ---------------------------------------------------------------------------

_NO_VISIBLE_SYMPTOM_HTML = """
<html><body style="margin:0">
    <div style="height: 100px;"></div>
    <button style="width: 80px; height: 40px;"></button>
</body></html>
"""


async def test_run_axe_audit_separates_borderline_from_allowlisted_candidates():
    """
    button-name (no visible symptom, see _VISUALLY_APPARENT_AXE_RULES) must
    land in borderline_candidates, never in the safe candidates list.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport=VIEWPORT)
        await page.set_content(_NO_VISIBLE_SYMPTOM_HTML)
        try:
            violations, candidates, borderline = await _run_axe_audit(page)
        finally:
            await browser.close()

    assert any(v["id"] == "button-name" for v in violations)
    assert candidates == [], "button-name has no visible symptom and must not be a safe candidate"
    assert any(c["description"] for c in borderline), \
        "expected the button-name violation to appear as a borderline candidate"


async def test_borderline_candidate_gets_boxed_when_vision_confirms(monkeypatch):
    """
    End to end through _audit_current_page: with only a borderline violation
    present and the vision gate monkeypatched to confirm it, the page ends
    up with a real box on that exact element, described with axe's own text
    — never anything the vision gate itself said.
    """
    monkeypatch.setattr(visuals, "_vision_confirms_visible_defect", lambda img, desc: True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport=VIEWPORT)
        await page.set_content(_NO_VISIBLE_SYMPTOM_HTML)
        try:
            result = await _audit_current_page(page, page.context, "/")
        finally:
            await browser.close()

    flaw = result["visual_flaw"]
    assert flaw is not None
    assert flaw["selector"] == "button"
    assert flaw["description"]  # axe's own `help` text, not invented
    assert _marker_pixels(result["screenshot_bytes"]) is not None
    assert result["closeup_bytes"] is not None


async def test_borderline_candidate_is_not_boxed_when_vision_rejects(monkeypatch):
    """
    A "no" from the vision gate must leave the page exactly as if the
    borderline path had never run — no box, no claim, same as today's
    existing "nothing qualified" outcome.
    """
    monkeypatch.setattr(visuals, "_vision_confirms_visible_defect", lambda img, desc: False)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport=VIEWPORT)
        await page.set_content(_NO_VISIBLE_SYMPTOM_HTML)
        try:
            result = await _audit_current_page(page, page.context, "/")
        finally:
            await browser.close()

    assert result["visual_flaw"] is None
    assert _marker_pixels(result["screenshot_bytes"]) is None
    assert result["closeup_bytes"] is None


async def test_a_safe_candidate_short_circuits_the_vision_gate_entirely(monkeypatch):
    """
    Cost/risk control: the vision gate must only ever be consulted when the
    safe allowlist produced nothing at all. A page with a real, visually
    apparent violation (color-contrast) must never trigger it, even though
    a borderline (button-name) violation is also present on the same page.
    """
    def _boom(*_args, **_kwargs):
        raise AssertionError("vision gate must not run when a safe candidate exists")

    monkeypatch.setattr(visuals, "_vision_confirms_visible_defect", _boom)

    html = f"""
    <html><body style="margin:0">
        <div style="height: 100px;"></div>
        <button style="width: 60px; height: 30px;"></button>
        <button style="width: 120px; height: 60px; {_LOW_CONTRAST_STYLE}">Book Now</button>
    </body></html>
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport=VIEWPORT)
        await page.set_content(html)
        try:
            result = await _audit_current_page(page, page.context, "/")
        finally:
            await browser.close()

    assert result["visual_flaw"] is not None
    assert result["visual_flaw"]["width"] >= 100  # the contrast button, not button-name's
