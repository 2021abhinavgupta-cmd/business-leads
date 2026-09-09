"""
Tests for the screenshot-accuracy pass.

Two separate complaints motivated this, and they need separate guards:
the IMAGE the prospect receives was not always what the email said it was,
and the COPY written about that image was not always true of it.

Five defects, one test section each:

1. Every screenshot email opened its evidence section with a false statement.
   BaseSender's caption was the hardcoded "Here is the screenshot my team
   took of your website on mobile:", while /api/send attached
   `<company>_<hash>_audit.jpg` — the DESKTOP capture. The mobile file
   (`_mobile.jpg`) existed on disk and was never attached to anything. This
   is the same bug CLAUDE.md §8 records as fixed in generate_followup on
   2026-08-09; that fix corrected the follow-up copy and never reached the
   first-touch template.

2. The red box could point at the wrong place. The screenshot was captured,
   then the bounding box was measured seconds later — after page.content(),
   the performance reads and axe-core's own scan. Playwright's own docs say
   bounding-box coordinates are only safe to reuse "assuming the page is
   static", and a carousel or a settling layout is not. The box is now drawn
   by the browser, in the same synchronous evaluate() that measures it.

3. The mobile screenshot was not a mobile screenshot — set_viewport_size on
   the desktop context, keeping the desktop user agent with is_mobile and
   has_touch False.

4. Visual claims were generated freely and judged afterwards by a second
   cheap vision model. They are now restricted to what the browser measured.

5. Captures were not deterministic: no animations="disabled", no consent
   banner suppression.

Unit tests only — no network, no API keys, no browser.
"""

import os
import re

import pytest
from playwright.async_api import async_playwright

import config
from analyzer import visuals
from analyzer.ai_audit import AIAuditor
from analyzer.flaws import Flaw
from analyzer.visuals import (
    make_mobile_screenshot_filename,
    make_screenshot_filename,
)
from emailer.ses_sender import SESSender
from scrapers.website import WebsiteData

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))


def _source(relative_path: str) -> str:
    """
    Read a module as text.

    Used instead of importing app.py, which performs a live Google Sheets
    authentication at module-import time (CLAUDE.md §8) and would couple these
    tests to network and credentials they have no business needing.
    """
    with open(os.path.join(_REPO_ROOT, relative_path), encoding="utf-8") as handle:
        return handle.read()


def _png(tmp_path, name):
    """Smallest thing PIL/MIMEImage will accept as a real image file."""
    path = tmp_path / name
    # 1x1 GIF — MIMEImage sniffs the type from the bytes, so this has to be a
    # genuinely valid image rather than arbitrary content.
    path.write_bytes(
        b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!"
        b"\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00"
        b"\x00\x02\x02D\x01\x00;"
    )
    return str(path)


def _captions(msg):
    """Every text/html part of a built message, concatenated."""
    return "\n".join(
        part.get_payload(decode=True).decode("utf-8")
        for part in msg.walk()
        if part.get_content_type() == "text/html"
    )


def _content_ids(msg):
    return [
        part.get("Content-ID")
        for part in msg.walk()
        if part.get_content_maintype() == "image"
    ]


def _real_web(**overrides):
    """A real WebsiteData — _build_prompt reads far more than any one field."""
    defaults = dict(
        url="https://example.com", reachable=True, load_time_ms=1200,
        page_speed_score=55, seo_score=70, mobile_score=60,
        has_cta=True, has_contact=True, has_testimonials=True, has_blog=True,
        has_ssl=True, meta_title="Acme", meta_description="We do things",
    )
    defaults.update(overrides)
    return WebsiteData(**defaults)


# ---------------------------------------------------------------------------
# 1. The email must not lie about which image it attached
# ---------------------------------------------------------------------------

def test_a_desktop_only_send_never_calls_the_image_a_mobile_screenshot(tmp_path):
    """The exact live falsehood: desktop bytes, 'on mobile' caption."""
    msg = SESSender()._build_initial_message(
        "lead@example.com", "S", "B", "<mid@x.com>",
        image_path=_png(tmp_path, "shot_audit.jpg"),
    )
    html = _captions(msg).lower()
    assert "on mobile" not in html
    assert "on a phone" not in html
    assert "desktop" in html


def test_the_old_hardcoded_mobile_caption_is_gone_from_the_source():
    """
    Belt and braces on the string itself. The caption is the thing that was
    wrong, and a future edit that reintroduces it would still pass a test
    that only checked one code path.
    """
    assert "screenshot my team took of your website on mobile" not in _source("emailer/base_sender.py")


def test_both_screenshots_are_attached_and_each_is_labelled_for_what_it_is(tmp_path):
    msg = SESSender()._build_initial_message(
        "lead@example.com", "S", "B", "<mid@x.com>",
        image_path=_png(tmp_path, "shot_audit.jpg"),
        mobile_image_path=_png(tmp_path, "shot_mobile.jpg"),
    )
    html = _captions(msg)
    assert "desktop browser" in html
    assert "on a phone" in html
    assert sorted(_content_ids(msg)) == ["<audit_img>", "<audit_img_mobile>"]


def test_every_caption_shown_has_a_matching_embedded_image(tmp_path):
    """
    A caption without its image renders as a broken placeholder in the
    recipient's inbox, over a sentence describing a picture they cannot see.
    """
    msg = SESSender()._build_initial_message(
        "lead@example.com", "S", "B", "<mid@x.com>",
        image_path=_png(tmp_path, "shot_audit.jpg"),
        mobile_image_path=_png(tmp_path, "shot_mobile.jpg"),
    )
    html = _captions(msg)
    embedded = {cid.strip("<>") for cid in _content_ids(msg)}
    referenced = set(re.findall(r"cid:([A-Za-z0-9_]+)", html))
    assert referenced == embedded


def test_a_mobile_path_that_was_never_written_is_dropped_not_captioned(tmp_path):
    """
    The mobile pass is best-effort and can fail while the desktop one
    succeeds. A path for a file that does not exist must produce neither a
    caption nor a dangling cid reference.
    """
    msg = SESSender()._build_initial_message(
        "lead@example.com", "S", "B", "<mid@x.com>",
        image_path=_png(tmp_path, "shot_audit.jpg"),
        mobile_image_path=str(tmp_path / "never_written_mobile.jpg"),
    )
    html = _captions(msg)
    assert "on a phone" not in html
    assert "audit_img_mobile" not in html
    assert _content_ids(msg) == ["<audit_img>"]


def test_a_send_with_no_images_at_all_is_unchanged(tmp_path):
    """The no-screenshot path is the majority case for leads with no site."""
    msg = SESSender()._build_initial_message("lead@example.com", "S", "B", "<mid@x.com>")
    assert _content_ids(msg) == []
    assert "Visual Audit Evidence" not in _captions(msg)


def test_send_email_forwards_the_mobile_screenshot_to_the_builder(tmp_path):
    """
    The parameter-drift guard. CLAUDE.md §8 records the /api/audit vs main.py
    version of exactly this: a new argument added at one call site and
    silently missing at another, leaving a whole feature dead in production.
    """
    sender = SESSender()
    seen = {}

    def _fake_build(to_email, subject, body, message_id, image_path=None,
                     mobile_image_path=None, closeup_image_path=None):
        seen["image_path"] = image_path
        seen["mobile_image_path"] = mobile_image_path
        seen["closeup_image_path"] = closeup_image_path
        raise RuntimeError("stop here — the arguments are what this test is about")

    sender._build_initial_message = _fake_build
    with pytest.raises(Exception):
        sender.send_email("lead@example.com", "S", "B",
                          image_path="/d_audit.jpg", mobile_image_path="/d_mobile.jpg",
                          closeup_image_path="/d_closeup.jpg")

    assert seen == {
        "image_path": "/d_audit.jpg",
        "mobile_image_path": "/d_mobile.jpg",
        "closeup_image_path": "/d_closeup.jpg",
    }


def test_the_send_endpoint_looks_up_both_screenshot_filenames():
    source = _source("app.py")
    assert "make_mobile_screenshot_filename" in source
    assert "mobile_image_path=mobile_image_path" in source


def test_the_approved_sender_also_forwards_both():
    """Third call site — the one most likely to be forgotten."""
    source = _source("send_approved.py")
    assert "mobile_image_path=mobile_image_path" in source


def test_the_send_endpoint_also_looks_up_the_closeup_crop():
    """
    Same parameter-drift guard as the mobile one above, for the close up
    crop added 2026-09-08 (make_closeup_screenshot_filename).
    """
    source = _source("app.py")
    assert "make_closeup_screenshot_filename" in source
    assert "closeup_image_path=closeup_image_path" in source


def test_the_approved_sender_also_forwards_the_closeup_crop():
    source = _source("send_approved.py")
    assert "closeup_image_path=closeup_image_path" in source


# ---------------------------------------------------------------------------
# Screenshot filenames — two files, one naming rule
# ---------------------------------------------------------------------------

def test_the_mobile_filename_pairs_with_the_desktop_one():
    desktop = make_screenshot_filename("Acme Yoga", "https://acme.example")
    mobile = make_mobile_screenshot_filename("Acme Yoga", "https://acme.example")
    assert desktop.endswith("_audit.jpg")
    assert mobile.endswith("_mobile.jpg")
    assert desktop[: -len("_audit.jpg")] == mobile[: -len("_mobile.jpg")]


def test_the_mobile_filename_is_still_collision_safe_across_urls():
    a = make_mobile_screenshot_filename("Acme", "https://one.example")
    b = make_mobile_screenshot_filename("Acme", "https://two.example")
    assert a != b


# ---------------------------------------------------------------------------
# 2. The red box is drawn by the browser, not painted on afterwards
# ---------------------------------------------------------------------------

def test_pillow_no_longer_draws_the_box():
    """
    ImageDraw is what made a stale coordinate possible: it paints a rectangle
    onto bytes captured at an earlier moment. Its absence is the fix.
    """
    source = _source("analyzer/visuals.py")
    assert "ImageDraw" not in source
    assert "draw.rectangle" not in source


def test_the_highlight_is_measured_and_drawn_in_one_evaluate():
    """
    Atomicity is the whole guarantee. If the rect were read in one round trip
    and the overlay appended in another, the gap would be back.
    """
    source = _source("analyzer/visuals.py")
    body = source.split("async def _highlight_element")[1].split("async def _remove_highlight")[0]
    assert body.count("page.evaluate") == 1
    assert "getBoundingClientRect()" in body
    assert "document.documentElement.appendChild(box)" in body


@pytest.mark.asyncio
async def test_no_candidates_means_no_box_and_no_browser_call():
    """
    Abstention: when axe-core found nothing highlightable, nothing is drawn
    and no red-box claim becomes available to the prompt.
    """
    class _Page:
        async def evaluate(self, *_args, **_kwargs):
            raise AssertionError("must not touch the page with nothing to highlight")

    assert await visuals._highlight_element(_Page(), []) is None


@pytest.mark.asyncio
async def test_a_browser_failure_while_highlighting_is_not_fatal():
    """A missing box costs one claim; a raise would cost the whole lead."""
    class _Page:
        async def evaluate(self, *_args, **_kwargs):
            raise RuntimeError("execution context destroyed")

    assert await visuals._highlight_element(_Page(), [{"selector": "a"}]) is None


@pytest.mark.asyncio
async def test_the_highlight_is_removed_before_the_other_checks_measure():
    """
    The overlay is a real element (now two: the box and its label, added
    2026-09-08). Left in place either would be counted by the font and
    stretched-image checks that run straight afterwards.
    """
    removed = {}

    class _Page:
        async def evaluate(self, script, arg=None):
            removed["script"] = script
            removed["arg"] = arg

    await visuals._remove_highlight(_Page())
    assert "remove()" in removed["script"]
    assert removed["arg"] == [visuals._HIGHLIGHT_OVERLAY_ID, visuals._HIGHLIGHT_LABEL_ID]


def test_the_axe_audit_returns_candidates_rather_than_coordinates():
    source = _source("analyzer/visuals.py")
    body = source.split("async def _run_axe_audit")[1].split("async def _check_font_consistency")[0]
    # The old implementation resolved a box per candidate with a round trip.
    assert "bounding_box" not in body
    assert '"selector"' in body


def test_root_elements_are_never_offered_as_highlight_candidates():
    """Boxing <body> is visually identical to boxing nothing."""
    source = _source("analyzer/visuals.py")
    assert '("html", "body", "head")' in source


def test_the_size_guard_is_area_based_not_an_and_of_both_dimensions():
    """
    The old guard required width AND height to be near-full, so a hero
    wrapper that was tall but slightly narrower than the viewport sailed
    through and highlighted essentially the whole screenshot — live
    reproduced on lp.zooty.in (CLAUDE.md §8).
    """
    source = _source("analyzer/visuals.py")
    assert "_MAX_HIGHLIGHT_AREA_FRACTION" in source
    assert "(rect.width * rect.height) > (vw * vh * maxArea)" in source
    assert visuals._MAX_HIGHLIGHT_AREA_FRACTION < 1.0


def test_an_offscreen_element_is_still_rejected():
    """
    The check moved into JavaScript; it must not have been lost on the way.
    An element below the fold returns a perfectly valid box whose coordinates
    are simply not in a viewport screenshot.
    """
    source = _source("analyzer/visuals.py")
    assert "rect.left < 0 || rect.top < 0" in source
    assert "rect.right > vw || rect.bottom > vh" in source


def test_an_invisible_element_is_rejected():
    """Present in the layout, not painted — a box around nothing visible."""
    source = _source("analyzer/visuals.py")
    for rule in ("style.visibility === 'hidden'", "style.display === 'none'",
                 "parseFloat(style.opacity) === 0"):
        assert rule in source


# ---------------------------------------------------------------------------
# 3. The mobile capture is genuinely mobile
# ---------------------------------------------------------------------------

def test_the_mobile_context_sets_every_property_a_device_needs():
    """
    Missing any one of these leaves it a narrow desktop window. is_mobile in
    particular is what enables Chromium's mobile viewport-meta behaviour.
    """
    device = visuals._MOBILE_DEVICE
    assert device["is_mobile"] is True
    assert device["has_touch"] is True
    assert device["device_scale_factor"] >= 2
    assert device["viewport"] == {"width": 390, "height": 844}
    assert "Mobile" in device["user_agent"]


def test_the_mobile_user_agent_matches_the_engine_we_actually_drive():
    """
    Chromium with an iOS Safari UA invites WebKit-specific CSS and JS into an
    engine that renders it differently — a new way for the capture to
    disagree with what a real visitor sees, in a pass whose entire job is
    fidelity.
    """
    ua = visuals._MOBILE_DEVICE["user_agent"]
    assert "Chrome/" in ua
    assert "Android" in ua
    assert "iPhone" not in ua


def test_the_mobile_pass_uses_its_own_context_not_a_resized_desktop_one():
    source = _source("analyzer/visuals.py")
    assert "browser.new_context(**_MOBILE_DEVICE)" in source
    # The old approach, which cannot set a user agent or is_mobile at all.
    assert 'set_viewport_size({"width": 390' not in source


def test_the_mobile_context_is_always_closed():
    """One leaked context per audit is a memory leak on a 500MB instance."""
    source = _source("analyzer/visuals.py")
    assert "await mobile_context.close()" in source


def test_the_mobile_capture_waits_for_fonts_and_scrolls_like_the_desktop_one():
    """
    It used to do neither, making it systematically the more under-rendered
    of the two images — and it is the one mobile claims are made about.
    """
    source = _source("analyzer/visuals.py")
    mobile_block = source.split("mobile_context = await browser.new_context")[1]
    assert "document.fonts.ready" in mobile_block
    assert "window.scrollTo(0, y)" in mobile_block


def test_mobile_overflow_is_measured_before_overlays_are_hidden():
    """
    A consent banner wider than the screen is a real horizontal overflow for
    a real visitor. Hiding it first would report the site as fine when it is
    not — the absent-signal direction this codebase keeps getting bitten by.
    """
    source = _source("analyzer/visuals.py")
    mobile_block = source.split("mobile_context = await browser.new_context")[1]
    overflow_at = mobile_block.index("scrollWidth > window.innerWidth")
    suppress_at = mobile_block.index("_suppress_overlays(mobile_page)")
    assert overflow_at < suppress_at


# ---------------------------------------------------------------------------
# 4. Visual claims are restricted to what was measured
# ---------------------------------------------------------------------------

def _prompt(web, has_image=True, has_mobile_image=True):
    return AIAuditor._build_prompt(
        "Acme", None, web, has_image=has_image, has_mobile_image=has_mobile_image,
    )


def test_the_model_is_forbidden_from_describing_anything_it_merely_sees():
    prompt = _prompt(_real_web())
    assert "ABSOLUTE RULE ABOUT THE IMAGES" in prompt
    assert "you may ONLY make a visual claim that appears in FLAWS" in prompt


def test_the_old_free_form_mobile_hunt_is_gone():
    """
    "look specifically for mobile only problems ... if you spot a mobile
    specific issue" asked the model to make an unverifiable perceptual claim
    about a picture the recipient is looking at while they read it.

    Asserted against the PROMPT rather than the source file: the wording is
    still quoted in a code comment explaining why it was removed, and what
    matters is what actually reaches the model.
    """
    prompt = _prompt(_real_web(), has_image=True, has_mobile_image=True)
    assert "look specifically for mobile only problems" not in prompt
    assert "If you spot a mobile specific issue" not in prompt
    # The other half of the same instruction: the model was told to go
    # looking in the image rather than to phrase what was measured.
    assert "what you can actually see in the image" not in prompt


def test_subjective_design_vocabulary_is_named_and_forbidden():
    """
    Naming the specific words works better than a general instruction — these
    are exactly what an unconstrained model reaches for about a screenshot.
    """
    prompt = _prompt(_real_web())
    for forbidden in ("colours", "cluttered", "dated", "professional", "horizontal scrolling"):
        assert forbidden in prompt


def test_a_mandatory_visual_critique_still_requires_real_evidence():
    """
    The 2026-08-09 rule is preserved: no evidence, no demand. A mandated
    critique with nothing measured is a hallucination generator.
    """
    with_evidence = _prompt(_real_web(visual_flaw_context="Red box around a low-contrast nav link"))
    without = _prompt(_real_web())
    assert "ONE OF YOUR FLAWS MUST BE A VISUAL CRITIQUE" in with_evidence
    assert "ONE OF YOUR FLAWS MUST BE A VISUAL CRITIQUE" not in without
    assert "Do NOT invent a visual criticism" in without


def test_a_mandated_visual_critique_must_still_carry_a_source_quote():
    """
    This is what makes the claim checkable by the machinery that already
    exists — _verify_source_quotes had nothing to match a design observation
    against, which is why visual claims floated free of every other guard.
    """
    prompt = _prompt(_real_web(visual_flaw_context="Red box around a low-contrast nav link"))
    assert "Quote its exact line in source_quote" in prompt


def test_the_prompt_says_which_image_is_which_when_both_are_attached():
    """"The red box" is ambiguous across two pictures."""
    both = _prompt(_real_web(), has_image=True, has_mobile_image=True)
    assert "TWO screenshots" in both
    assert "name which one you mean" in both.replace("\n", " ")


def test_the_prompt_does_not_promise_two_images_when_only_one_is_sent():
    one = _prompt(_real_web(), has_image=True, has_mobile_image=False)
    assert "TWO screenshots" not in one
    assert "DESKTOP screenshot" in one


def test_no_image_means_no_image_rules_at_all():
    none = _prompt(_real_web(), has_image=False, has_mobile_image=False)
    assert "ABSOLUTE RULE ABOUT THE IMAGES" not in none
    assert "ONE OF YOUR FLAWS MUST BE A VISUAL CRITIQUE" not in none


def test_both_red_boxes_are_described_when_both_exist():
    prompt = _prompt(_real_web(
        visual_flaw_context="The red box in the desktop screenshot highlights X.",
        mobile_visual_flaw_context="The red box in the mobile screenshot highlights Y.",
    ))
    assert "desktop screenshot highlights X" in prompt
    assert "mobile screenshot highlights Y" in prompt


# ---------------------------------------------------------------------------
# _has_visual_evidence — what now counts as measured
# ---------------------------------------------------------------------------

def test_a_mobile_only_red_box_counts_as_visual_evidence():
    web = _real_web(mobile_visual_flaw_context="The red box in the mobile screenshot highlights Y.")
    assert AIAuditor._has_visual_evidence(web)


def test_measured_mobile_overflow_counts_as_visual_evidence():
    """
    The one thing that licenses a mobile claim now that the model may not go
    looking for one in the image.
    """
    web = _real_web(flaws=[Flaw(
        "performance", "high",
        "Page requires horizontal scrolling on a mobile phone screen — some content is wider than the viewport.",
    )])
    assert AIAuditor._has_visual_evidence(web)


def test_a_slow_but_visually_clean_site_still_has_no_visual_evidence():
    web = _real_web(flaws=[Flaw("performance", "high", "Largest Contentful Paint takes 6.2s to render.")])
    assert not AIAuditor._has_visual_evidence(web)


def test_unrenderable_text_still_overrides_every_other_kind_of_evidence():
    """
    A browser with no fonts screenshots images and shapes with every word
    blank. Any "visual" claim from that image describes our container.
    """
    web = _real_web(
        visual_flaw_context="Red box around something",
        mobile_visual_flaw_context="Red box in the mobile shot too",
        signal_status={"screenshot_text_rendering": "no_data"},
    )
    assert not AIAuditor._has_visual_evidence(web)


def test_the_mobile_flaw_context_reaches_the_website_data():
    """The plumbing, not the policy — a field nothing populates is dead."""
    source = _source("scrapers/website.py")
    assert 'mobile_visual_flaw_context=extra.get("mobile_visual_flaw_context", "")' in source


# ---------------------------------------------------------------------------
# 5. Deterministic capture
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_every_capture_disables_animations_and_hides_the_caret():
    """
    Playwright fast-forwards finite animations to completion under
    animations="disabled", which is what stops an entrance fade being caught
    at whatever opacity it happened to be at. The manual scroll pass cannot
    do this for a timed cross-fade or a slider between slides.
    """
    captured = {}

    class _Page:
        async def screenshot(self, **kwargs):
            captured.update(kwargs)
            return b"bytes"

    assert await visuals._capture(_Page()) == b"bytes"
    assert captured["animations"] == "disabled"
    assert captured["caret"] == "hide"
    assert captured["full_page"] is False


@pytest.mark.asyncio
async def test_capture_closeup_pads_a_small_element_instead_of_cropping_it_bare():
    """
    A checkbox-sized violation (the smallest this pipeline ever highlights,
    per _MIN_HIGHLIGHT_PX=12) must not produce a 12x12 pixel crop — that is
    technically a "close up" but shows nothing a recipient could recognise
    as their own page. The crop should be padded out to at least
    _CLOSEUP_MIN_SIZE_PX, centered on the element.
    """
    captured = {}

    class _Page:
        viewport_size = {"width": 1280, "height": 800}

        async def screenshot(self, **kwargs):
            captured.update(kwargs)
            return b"bytes"

    rect = {"x": 600, "y": 400, "width": 12, "height": 12}
    result = await visuals._capture_closeup(_Page(), rect)

    assert result == b"bytes"
    clip = captured["clip"]
    assert clip["width"] == visuals._CLOSEUP_MIN_SIZE_PX
    assert clip["height"] == visuals._CLOSEUP_MIN_SIZE_PX
    # Centered on the element's own center point.
    element_cx, element_cy = 606, 406
    assert abs((clip["x"] + clip["width"] / 2) - element_cx) < 1
    assert abs((clip["y"] + clip["height"] / 2) - element_cy) < 1
    assert captured["animations"] == "disabled"
    assert captured["caret"] == "hide"


@pytest.mark.asyncio
async def test_capture_closeup_pads_a_large_element_by_a_fixed_margin():
    """A big element doesn't need padding out to the floor — just the fixed margin on each side."""
    captured = {}

    class _Page:
        viewport_size = {"width": 1280, "height": 800}

        async def screenshot(self, **kwargs):
            captured.update(kwargs)
            return b"bytes"

    rect = {"x": 100, "y": 100, "width": 300, "height": 200}
    await visuals._capture_closeup(_Page(), rect)

    clip = captured["clip"]
    assert clip["width"] == 300 + visuals._CLOSEUP_PADDING_PX * 2
    assert clip["height"] == 200 + visuals._CLOSEUP_PADDING_PX * 2


@pytest.mark.asyncio
async def test_capture_closeup_clamps_to_the_viewport_near_an_edge():
    """
    An element right against the top left corner would otherwise center a
    padded crop partly off screen (negative x/y) — content that was never
    in the captured viewport at all. The crop must stay fully inside it.
    """
    captured = {}

    class _Page:
        viewport_size = {"width": 1280, "height": 800}

        async def screenshot(self, **kwargs):
            captured.update(kwargs)
            return b"bytes"

    rect = {"x": 0, "y": 0, "width": 20, "height": 20}
    await visuals._capture_closeup(_Page(), rect)

    clip = captured["clip"]
    assert clip["x"] >= 0
    assert clip["y"] >= 0
    assert clip["x"] + clip["width"] <= 1280
    assert clip["y"] + clip["height"] <= 800


@pytest.mark.asyncio
async def test_capture_closeup_never_exceeds_the_viewport_itself():
    """A crop wider or taller than the actual captured viewport is nonsensical."""
    captured = {}

    class _Page:
        viewport_size = {"width": 390, "height": 200}

        async def screenshot(self, **kwargs):
            captured.update(kwargs)
            return b"bytes"

    rect = {"x": 10, "y": 10, "width": 20, "height": 20}
    await visuals._capture_closeup(_Page(), rect)

    clip = captured["clip"]
    assert clip["width"] <= 390
    assert clip["height"] <= 200


@pytest.mark.asyncio
async def test_capture_closeup_degrades_to_none_without_a_viewport():
    """No viewport_size (an unusual page/browser state) must not raise."""
    class _Page:
        viewport_size = None

    assert await visuals._capture_closeup(_Page(), {"x": 0, "y": 0, "width": 10, "height": 10}) is None


def test_nothing_screenshots_the_page_without_going_through_capture():
    """
    A second, undocumented raw page.screenshot() call would silently
    reintroduce non-deterministic capture for whichever image it produced.

    Exactly two call sites are legitimate: _capture (the full page) and
    _capture_closeup (a padded crop around the marked element, taken while
    it's still highlighted — rewritten 2026-09-08 to pad the crop rather
    than screenshot the bare element, see _CLOSEUP_PADDING_PX). Both are
    pinned here by name and both carry the same animations/caret
    determinism options — a THIRD call site anywhere else in the file is
    what this test exists to catch.
    """
    source = _source("analyzer/visuals.py")
    # "await" restricts this to real invocations — a docstring merely
    # mentioning ".screenshot(" in prose (as _capture_closeup's now does,
    # explaining why it uses page.screenshot(clip=...) over an element
    # handle's own) is not a call site.
    raw = [
        line for line in source.splitlines()
        if ".screenshot(" in line and "await" in line and "async def screenshot" not in line
    ]
    assert len(raw) == 2, f"expected only _capture and _capture_closeup to screenshot, found: {raw}"
    assert any("full_page=False, animations=\"disabled\", caret=\"hide\"" in line for line in raw)
    closeup_body = source.split("async def _capture_closeup")[1].split("async def ")[0]
    assert "page.screenshot(" in closeup_body
    assert 'clip={"x": x, "y": y, "width": crop_w, "height": crop_h}' in closeup_body
    assert 'animations="disabled", caret="hide"' in closeup_body


@pytest.mark.asyncio
async def test_overlay_suppression_never_fails_an_audit():
    """A site with no consent banner is the normal case, not an error."""
    class _Page:
        async def add_style_tag(self, **_kwargs):
            raise RuntimeError("CSP blocked the style tag")

    await visuals._suppress_overlays(_Page())  # must not raise


@pytest.mark.asyncio
async def test_overlay_suppression_hides_consent_and_chat_widgets():
    seen = {}

    class _Page:
        async def add_style_tag(self, content=""):
            seen["content"] = content

    await visuals._suppress_overlays(_Page())
    assert "display: none !important" in seen["content"]
    assert "#onetrust-banner-sdk" in seen["content"]
    assert "#intercom-container" in seen["content"]


@pytest.mark.asyncio
async def test_the_expanded_selector_list_is_valid_css_in_a_real_browser():
    """
    Every entry added 2026-09-08 (Usercentrics, TrustArc, Quantcast Choice,
    Sourcepoint, Iubenda, Termly, Borlabs, Osano, Didomi, Klaro, Google
    Funding Choices, Civic Cookie Control, Cookie Information, CookieFirst,
    Ketch, generic gdpr/consent-banner substrings, and a handful more chat
    widgets) has to be real, parseable CSS or add_style_tag raises and the
    whole suppression pass — which _run_axe_audit depends on running before
    it — degrades silently for every site, not just ones with a matching
    vendor. A syntax slip here would be invisible without this test: it
    can't be caught by the mocked unit tests above, which never touch a
    real CSS parser.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content("<html><body></body></html>")
        await visuals._suppress_overlays(page)  # must not raise
        await browser.close()


@pytest.mark.asyncio
async def test_the_expanded_selector_list_actually_hides_the_new_vendors():
    """A sample of the newly added selectors, checked against real matching markup."""
    html = """
    <html><body>
        <div id="usercentrics-root">consent banner</div>
        <div class="klaro">consent banner</div>
        <div id="ccc">civic cookie control</div>
        <div id="my-gdpr-notice">generic gdpr substring match</div>
        <div id="chat-widget-container">chat</div>
        <button id="real-cta">Book Now</button>
    </body></html>
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(html)
        await visuals._suppress_overlays(page)
        hidden = await page.evaluate(
            """() => ['usercentrics-root', 'ccc', 'my-gdpr-notice', 'chat-widget-container']
                .map(id => getComputedStyle(document.getElementById(id)).display)"""
        )
        klaro_hidden = await page.evaluate(
            "() => getComputedStyle(document.querySelector('.klaro')).display"
        )
        cta_display = await page.evaluate(
            "() => getComputedStyle(document.getElementById('real-cta')).display"
        )
        await browser.close()

    assert all(d == "none" for d in hidden), hidden
    assert klaro_hidden == "none"
    # The site's own real call to action must never be caught by a substring
    # guess — it isn't named anything cookie/consent/gdpr-related here.
    assert cta_display != "none"


def test_overlays_are_hidden_before_axe_runs_not_only_before_the_capture():
    """
    So the violations reported and the image attached describe the same page.
    A violation raised inside a widget we then hid would be a finding the
    recipient cannot see and did not author.
    """
    source = _source("analyzer/visuals.py")
    flow = source.split("async def _generate_audit_screenshot_once")[1]
    suppress_at = flow.index("await _suppress_overlays(page)")
    audit_at = flow.index('_audit_current_page(page, context, "/")')
    assert suppress_at < audit_at


def test_the_page_html_is_captured_before_anything_is_hidden():
    """
    Downstream parsing (contact emails, phone numbers, schema) must see the
    real page, not our stripped-down version of it.
    """
    source = _source("analyzer/visuals.py")
    flow = source.split("async def _generate_audit_screenshot_once")[1]
    assert flow.index("html_content = await page.content()") < flow.index("await _suppress_overlays(page)")


def test_the_capture_happens_inside_the_per_page_audit():
    """
    The screenshot used to be taken by the caller and passed down, which is
    the gap that let the image and the box describe different moments.
    """
    source = _source("analyzer/visuals.py")
    signature = source.split("async def _audit_current_page(")[1].split(")")[0]
    assert "screenshot_bytes" not in signature
    body = source.split("async def _audit_current_page")[1].split("def _consolidate_by_key")[0]
    # rindex (not index) for the last two markers, since 2026-09-09's
    # vision-gated borderline-candidate fallback (see
    # _vision_confirms_visible_defect) legitimately adds an EARLIER
    # _remove_highlight(page) call — cleaning up a borderline highlight
    # vision rejected, before the real capture ever happens. What still
    # must hold, and is what this asserts: the safe-candidate highlight
    # happens before the real page.screenshot(), which happens before the
    # function's final, unconditional cleanup call.
    first_highlight = body.index("_highlight_element(page, candidates)")
    last_capture = body.rindex("_capture(page)")
    last_remove = body.rindex("_remove_highlight(page)")
    assert first_highlight < last_capture < last_remove


# ---------------------------------------------------------------------------
# Vision-gated borderline candidates (added 2026-09-09) — see
# _vision_confirms_visible_defect's own docstring for why this is safe to
# add on top of the 2026-08-31 "vision no longer freely critiques" fix.
# ---------------------------------------------------------------------------

def test_vision_gate_returns_false_with_no_provider_configured(monkeypatch):
    """
    Fails closed to the existing "no candidates" outcome, never to a raise —
    same silent-degradation contract as every AI call in this codebase.
    """
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")

    def _boom(*_args, **_kwargs):
        raise AssertionError("must not attempt any provider call with none configured")

    monkeypatch.setattr(visuals, "_call_vision_gate_raw", _boom)
    assert visuals._vision_confirms_visible_defect(b"fake-bytes", "some flaw") is False


def test_vision_gate_true_on_a_clean_confirmation(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(visuals, "_call_vision_gate_raw", lambda p, img: '{"visible": true}')
    assert visuals._vision_confirms_visible_defect(b"fake-bytes", "low contrast text") is True


def test_vision_gate_false_on_an_explicit_rejection(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(visuals, "_call_vision_gate_raw", lambda p, img: '{"visible": false}')
    assert visuals._vision_confirms_visible_defect(b"fake-bytes", "low contrast text") is False


def test_vision_gate_false_when_the_provider_returns_nothing(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(visuals, "_call_vision_gate_raw", lambda p, img: None)
    assert visuals._vision_confirms_visible_defect(b"fake-bytes", "low contrast text") is False


@pytest.mark.parametrize("raw", [
    "not json at all",
    "```json\n{not valid}\n```",
    '{"something_else": true}',
    "",
])
def test_vision_gate_false_on_unusable_responses(monkeypatch, raw):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(visuals, "_call_vision_gate_raw", lambda p, img: raw)
    assert visuals._vision_confirms_visible_defect(b"fake-bytes", "low contrast text") is False


def test_vision_gate_strips_markdown_fences_around_valid_json(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(visuals, "_call_vision_gate_raw", lambda p, img: '```json\n{"visible": true}\n```')
    assert visuals._vision_confirms_visible_defect(b"fake-bytes", "low contrast text") is True


def test_vision_gate_prompt_includes_the_axe_description_and_asks_a_narrow_question(monkeypatch):
    """
    The prompt must cite the SPECIFIC flaw axe found (so the model judges
    that, not an open-ended "find something wrong") and must not ask the
    model to describe or invent anything of its own.
    """
    captured = {}

    def _capture_call(prompt, image_bytes):
        captured["prompt"] = prompt
        return '{"visible": true}'

    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(visuals, "_call_vision_gate_raw", _capture_call)
    visuals._vision_confirms_visible_defect(b"fake-bytes", "Elements must meet minimum color contrast ratio thresholds")
    assert "Elements must meet minimum color contrast ratio thresholds" in captured["prompt"]
    assert "true or false" in captured["prompt"]


def test_vision_gate_raw_tries_gemini_then_claude_then_openai_in_order(monkeypatch):
    """Same fixed preference order as AIAuditor._call_vision_judge."""
    calls = []

    class _FakeGeminiModel:
        def __init__(self, *a, **k):
            pass
        def generate_content(self, *_a, **_k):
            calls.append("gemini")
            raise RuntimeError("gemini down")

    class _FakeAnthropicMessage:
        content = [type("C", (), {"text": '{"visible": true}'})()]

    class _FakeAnthropicClient:
        def __init__(self, *a, **k):
            pass
        class messages:
            @staticmethod
            def create(*_a, **_k):
                calls.append("anthropic")
                return _FakeAnthropicMessage()

    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")

    import google.generativeai as genai
    monkeypatch.setattr(genai, "GenerativeModel", _FakeGeminiModel)
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropicClient)

    raw = visuals._call_vision_gate_raw("prompt", b"fake-bytes")
    assert calls == ["gemini", "anthropic"]
    assert raw == '{"visible": true}'
