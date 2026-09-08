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


def test_nothing_screenshots_the_page_without_going_through_capture():
    """
    A second, undocumented raw page/element .screenshot() call would silently
    reintroduce non-deterministic capture for whichever image it produced.

    Exactly two call sites are legitimate: _capture (the full page) and
    _capture_closeup (added 2026-09-08, a tight crop of the marked element,
    taken while it's still highlighted). Both are pinned here by name and
    both carry the same animations/caret determinism options — a THIRD call
    site anywhere else in the file is what this test exists to catch.
    """
    source = _source("analyzer/visuals.py")
    raw = [
        line for line in source.splitlines()
        if ".screenshot(" in line and "async def screenshot" not in line
    ]
    assert len(raw) == 2, f"expected only _capture and _capture_closeup to screenshot, found: {raw}"
    assert any("full_page=False, animations=\"disabled\", caret=\"hide\"" in line for line in raw)
    assert any('el.screenshot(animations="disabled", caret="hide")' in line for line in raw)


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
    order = [body.index(marker) for marker in (
        "_highlight_element(page, candidates)", "_capture(page)", "_remove_highlight(page)",
    )]
    assert order == sorted(order)
