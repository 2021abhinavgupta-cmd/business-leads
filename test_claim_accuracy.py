"""
Tests for three claims that shipped to a real lead and were all false
(added 2026-08-10).

Every accuracy check that existed at the time read the SAME audit data the
drafting model read, so a fact that was already wrong when it reached the
prompt passed all of them. The AI faithfully repeated three bad inputs:

  1. "your phone number doesn't match your Google listing" — _PHONE_PATTERN
     could only consume 8 digits after the country code, so it truncated
     "+91 86559 30022" to "+91 86559 3002". nap_check compares the LAST 8
     digits, so the truncation shifted the comparison window and a site whose
     number matched its listing exactly was reported as a mismatch.
  2. "9 broken links ... like the anchor link to your content section" — the
     scan read `a.href` (DOM-resolved), so <a href="#content"> arrived as an
     absolute URL and was probed as an outbound link. Re-probing the site
     found 0 of 25 assets broken; across all 111 links and images the only
     failure was facebook.com, which returns 400 to both HEAD and GET because
     it blocks bots, not because the link is dead.
  3. "about 5 seconds ... for visitors on mobile or slower connections" — a
     desktop, unthrottled measurement. Throttled mobile LCP was 15.9s.

And one that made the screenshot itself wrong: python:3.11-slim installs no
fonts, so Chromium rendered images and shapes but no text at all.

Unit tests only — no network, no browser.
"""

import re

import pytest

from analyzer.nap_check import normalise_phone, phone_mismatch


# ---------------------------------------------------------------------------
# 1. Phone extraction
# ---------------------------------------------------------------------------

def _extract(text):
    from scrapers.website import _extract_phones

    return _extract_phones(text)


def test_a_five_five_grouped_indian_mobile_is_not_truncated():
    """The exact number from the live site that produced the false claim."""
    assert _extract("call +91 86559 30022 now") == ["+91 86559 30022"]


@pytest.mark.parametrize("raw", [
    "+91 86559 30022",
    "+91 98765 43210",
    "+91-88888-77777",
    "+91 9876543210",
    "022 2640 1234",
    "(022) 2640 1234",
    "9876543210",
    "+1 (415) 555-2671",
])
def test_common_formats_are_captured_whole(raw):
    found = _extract(f"contact us on {raw} today")
    assert found, f"{raw} was not matched at all"
    assert re.sub(r"\D", "", found[0]) == re.sub(r"\D", "", raw), (
        f"{raw} captured as {found[0]!r} — digits were lost"
    )


@pytest.mark.parametrize("noise", [
    "GST 27AAECS1234F1Z5",
    "copyright 2024 all rights reserved",
    "open 9 30 am to 6 30 pm",
])
def test_non_phone_digit_runs_are_not_captured(noise):
    """The pattern is permissive, so the digit-count bounds do the filtering."""
    assert _extract(noise) == []


def test_a_matching_number_no_longer_reports_a_mismatch():
    """
    End to end over the real bug: the site prints the same number Google
    lists, so nothing should be reported.
    """
    site_text = "The Yoga House, Bandra (W), Mumbai +91 86559 30022 info@x.in"
    assert phone_mismatch("+91 86559 30022", _extract(site_text)) is None


def test_a_genuinely_different_number_is_still_reported():
    """The fix must not blunt the check."""
    site_text = "reach us on +91 99999 11111"
    assert phone_mismatch("+91 86559 30022", _extract(site_text)) == "+91 86559 30022"


def test_differently_formatted_but_identical_numbers_agree():
    """This is what truncation broke: same number, two spellings."""
    assert normalise_phone("+91 86559 30022") == normalise_phone("+918655930022")


# ---------------------------------------------------------------------------
# 2. Broken links
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://facebook.com/theyogahousemumbai",
    "https://www.facebook.com/x",
    "https://m.facebook.com/x",
    "https://www.instagram.com/theyogahouse/",
    "https://wa.me/919876543210",
    "https://x.com/someone",
])
def test_bot_blocking_hosts_are_excluded_from_the_scan(url):
    """
    These answer 400/403/429 to any automated client. That says "you are not
    a browser", not "this link is dead", and the probe cannot tell the
    difference — so they must not be probed at all.
    """
    from analyzer.visuals import _is_bot_hostile

    assert _is_bot_hostile(url) is True


@pytest.mark.parametrize("url", [
    "https://www.yogahouse.in/courses/",
    "https://cdn.example.com/hero.png",
    "https://notfacebook.com/page",
])
def test_ordinary_links_are_still_probed(url):
    from analyzer.visuals import _is_bot_hostile

    assert _is_bot_hostile(url) is False


def test_same_page_fragments_are_filtered_in_the_extractor():
    """
    `a.href` is DOM-resolved, so <a href="#content"> becomes an absolute URL
    and passes a startsWith('http') filter. The extraction script has to
    compare the RAW attribute, or a skip-link gets reported to a business
    owner as a link that goes nowhere.
    """
    import inspect

    from analyzer import visuals

    source = inspect.getsource(visuals._check_broken_assets)
    assert "getAttribute('href')" in source, (
        "the raw href attribute must be consulted; a.href alone cannot "
        "distinguish a fragment from an outbound link"
    )
    assert "isSamePageFragment" in source


def test_bot_hostile_hosts_are_filtered_before_probing():
    import inspect

    from analyzer import visuals

    source = inspect.getsource(visuals._check_broken_assets)
    assert "_is_bot_hostile" in source


# ---------------------------------------------------------------------------
# 3. Speed provenance
# ---------------------------------------------------------------------------

_FLAW_BASE = dict(
    load_time_ms=1200, perf_score=57, seo_score=77, mobile_score=60,
    best_practices_score=80, has_ssl=True,
    parsed={"has_cta": True, "has_contact": True, "has_testimonials": True,
            "has_blog": True, "meta_title": "Acme", "meta_description": "d",
            "h1_tags": ["h"], "homepage_text": "x"},
    has_structured_data=True, readability_score=60, security_flaws=[],
    seo_page={}, accessibility_violations=[], broken_links=[],
)


def _lcp_flaw(source):
    from scrapers.website import WebsiteScraper

    flaws = WebsiteScraper._build_flaws(lcp_ms=4082, lcp_source=source, **_FLAW_BASE)
    return next(f.description for f in flaws if "Contentful" in f.description)


def test_a_desktop_measurement_says_so():
    """
    The three sources measure different things: CrUX is real visitors,
    real_web_vitals is our own DESKTOP browser on a datacenter connection,
    and Lighthouse is a throttled mobile lab run. Quoting a desktop number as
    what mobile visitors experience is what happened on the real lead, where
    desktop LCP was 4.1s and throttled mobile was 15.9s.
    """
    text = _lcp_flaw("real_web_vitals")
    assert "desktop" in text
    assert "phone visitors will be slower" in text


def test_a_field_measurement_says_it_is_real_visitors():
    assert "real visitors" in _lcp_flaw("CrUX")


def test_a_lab_measurement_says_it_is_simulated():
    text = _lcp_flaw("lighthouse")
    assert "simulated" in text and "slow-4G" in text


def test_an_unknown_source_claims_no_device():
    """Better to say nothing than to attribute the number to the wrong thing."""
    text = _lcp_flaw("")
    for word in ("desktop", "mobile", "real visitors", "simulated"):
        assert word not in text


def test_the_number_itself_is_unchanged_by_provenance():
    for source in ("CrUX", "real_web_vitals", "lighthouse", ""):
        assert "4.1s" in _lcp_flaw(source)


# ---------------------------------------------------------------------------
# 4. Screenshot text rendering
# ---------------------------------------------------------------------------

def test_the_docker_image_installs_fonts():
    """
    python:3.11-slim ships no fonts. Chromium then renders images, shapes and
    colours normally while every text node comes out blank — and that image is
    attached to the outgoing email AND fed to the vision model. The dependency
    list here was hand-written instead of taken from `playwright install
    --with-deps`, which is how the fonts came to be missing.
    """
    dockerfile = open("Dockerfile", encoding="utf-8").read()
    for package in ("fonts-liberation", "fonts-dejavu-core", "fonts-noto-core"):
        assert package in dockerfile, f"{package} missing — text will not render"


def test_devanagari_coverage_is_installed():
    """Leads are Indian businesses; Liberation and DejaVu have no Devanagari."""
    dockerfile = open("Dockerfile", encoding="utf-8").read()
    assert "fonts-indic" in dockerfile or "fonts-noto-core" in dockerfile


def test_unrenderable_text_suppresses_the_visual_critique():
    """
    With no fonts, every "visual flaw" visible in the screenshot is a fact
    about our container, not the prospect's site. The prompt must not demand
    a visual criticism from that image.
    """
    from analyzer.ai_audit import AIAuditor
    from scrapers.website import WebsiteData

    web = WebsiteData(
        url="https://example.com", reachable=True, load_time_ms=1200,
        page_speed_score=55, seo_score=70, mobile_score=60,
        has_cta=True, has_contact=True, has_testimonials=True, has_blog=True,
        has_ssl=True, meta_title="Acme", meta_description="We do things",
        visual_flaw_context="a red box was drawn around something",
        signal_status={"screenshot_text_rendering": "no_data"},
    )
    assert AIAuditor._has_visual_evidence(web) is False


def test_visual_evidence_still_counts_when_text_renders():
    from analyzer.ai_audit import AIAuditor
    from scrapers.website import WebsiteData

    web = WebsiteData(
        url="https://example.com", reachable=True, load_time_ms=1200,
        page_speed_score=55, seo_score=70, mobile_score=60,
        has_cta=True, has_contact=True, has_testimonials=True, has_blog=True,
        has_ssl=True, meta_title="Acme", meta_description="We do things",
        visual_flaw_context="a red box was drawn around something",
        signal_status={},
    )
    assert AIAuditor._has_visual_evidence(web) is True


def test_the_screenshot_waits_for_fonts_and_scrolls():
    """
    document.fonts.ready is the browser's own signal that @font-face loads
    resolved; the scroll pass triggers IntersectionObserver entrance
    animations and lazy image decoding that never fire for a browser that
    stays at the top of the page.
    """
    import inspect

    from analyzer import visuals

    # The public entry point is a retry wrapper; the capture itself lives in
    # _generate_audit_screenshot_once.
    source = inspect.getsource(visuals._generate_audit_screenshot_once)
    assert "document.fonts.ready" in source
    assert "window.scrollTo" in source
    assert "_can_render_text" in source


# ---------------------------------------------------------------------------
# 5. The prompt can finally see what the claims are about
# ---------------------------------------------------------------------------

def _web_with(**kwargs):
    from scrapers.website import WebsiteData

    base = dict(
        url="https://example.com", reachable=True, load_time_ms=1200,
        page_speed_score=55, seo_score=70, mobile_score=60,
        has_cta=True, has_contact=True, has_testimonials=True, has_blog=True,
        has_ssl=True, meta_title="Acme", meta_description="We do things",
    )
    base.update(kwargs)
    return WebsiteData(**base)


def test_phone_numbers_found_on_the_site_reach_the_prompt():
    """
    The NAP flaw asserts a fact about these numbers. Nothing downstream could
    check it, because on the real lead the number sat at character 6263 of
    the page and the prompt carried only the first 2000.
    """
    from analyzer.ai_audit import AIAuditor

    prompt = AIAuditor._build_prompt("Acme", None, _web_with(site_phones=["+91 86559 30022"]))
    assert "+91 86559 30022" in prompt


def test_no_phone_line_is_added_when_none_were_found():
    """Absence is not evidence — don't invite a claim about it."""
    from analyzer.ai_audit import AIAuditor

    prompt = AIAuditor._build_prompt("Acme", None, _web_with(site_phones=[]))
    assert "Phone numbers printed on the site" not in prompt


def test_the_prompt_carries_more_than_the_old_2000_characters():
    from analyzer.ai_audit import AIAuditor
    from analyzer import ai_audit

    body = "word " * 3000
    prompt = AIAuditor._build_prompt("Acme", None, _web_with(homepage_text=body))
    assert ai_audit._PROMPT_HOMEPAGE_CHARS > 2000
    assert len(prompt) > 2000 + 1000


def test_the_scraper_keeps_enough_text_to_reach_a_footer():
    from scrapers import website

    assert website._HOMEPAGE_TEXT_CHARS >= 8000, (
        "the phone/address/hours a NAP claim is about live in the footer"
    )


def test_readability_is_still_scored_on_the_original_sample():
    """Widening homepage_text must not silently recalibrate this threshold."""
    from scrapers import website

    assert website._READABILITY_SAMPLE_CHARS == 3000
