"""
Tests for the two new free signals added 2026-08-09, plus the two wiring
bugs found alongside them.

NAP consistency (analyzer/nap_check.py) compares the website's own contact
details against the Google Business Profile the lead was scraped from. Both
sides were already in hand, so this costs no new API call. SSL expiry
(analyzer/ssl_expiry.py) turns the existing boolean has_ssl into a
countdown, using only stdlib ssl/socket.

Both are deliberately conservative: they must fire on a real contradiction
and stay silent on a missing signal, since "we couldn't find it" becoming
"you don't have it" is the exact bug class this codebase keeps fixing.

Unit tests only — no network, no real TLS handshake, no API keys.
"""

import pytest

from analyzer import nap_check
from scrapers.website import WebsiteScraper


# ---------------------------------------------------------------------------
# Phone matching
# ---------------------------------------------------------------------------

def test_same_number_written_differently_is_not_a_mismatch():
    """+91 98765 43210 and 098765-43210 are the same phone."""
    assert nap_check.phone_mismatch("+91 98765 43210", ["098765-43210"]) is None


def test_a_genuinely_different_number_is_a_mismatch():
    assert nap_check.phone_mismatch("+91 98765 43210", ["+91 90000 11111"]) == "+91 98765 43210"


def test_any_matching_number_on_the_page_clears_the_flag():
    """Sites list several numbers; one matching the Google listing is enough."""
    phones = ["+91 90000 11111", "022 1234 5678", "98765 43210"]
    assert nap_check.phone_mismatch("+91 98765 43210", phones) is None


def test_no_phone_found_on_the_page_is_never_reported():
    """
    Absence is not evidence: the number is usually in an image, a JS widget,
    or a contact page we didn't crawl.
    """
    assert nap_check.phone_mismatch("+91 98765 43210", []) is None


def test_no_google_phone_means_nothing_to_compare():
    assert nap_check.phone_mismatch("", ["+91 98765 43210"]) is None


def test_too_short_to_compare_is_not_a_mismatch():
    """A 4-digit extension isn't enough to call anything a contradiction."""
    assert nap_check.phone_mismatch("1234", ["+91 98765 43210"]) is None


def test_normalise_phone_keeps_only_significant_trailing_digits():
    assert nap_check.normalise_phone("+91 98765 43210") == nap_check.normalise_phone("098765 43210")


# ---------------------------------------------------------------------------
# Address matching — loose on purpose
# ---------------------------------------------------------------------------

def test_same_address_formatted_differently_is_not_a_mismatch():
    gbp = "Shop 4, 100 Feet Road, Indiranagar, Bengaluru, Karnataka 560038, India"
    page = "visit us at indiranagar, 100 feet road, bengaluru 560038 open daily"
    assert nap_check.address_mismatch(gbp, page) is None


def test_a_completely_different_address_is_a_mismatch():
    gbp = "Shop 4, 100 Feet Road, Indiranagar, Bengaluru, Karnataka 560038, India"
    page = "our office is in Bandra West, Mumbai, Maharashtra 400050"
    assert nap_check.address_mismatch(gbp, page) == gbp


def test_empty_page_text_is_never_a_mismatch():
    gbp = "Shop 4, 100 Feet Road, Indiranagar, Bengaluru, Karnataka 560038, India"
    assert nap_check.address_mismatch(gbp, "") is None


def test_too_vague_a_google_address_is_skipped():
    """Nothing meaningful to match on shouldn't produce a confident claim."""
    assert nap_check.address_mismatch("India", "some unrelated page text here") is None


def test_stopwords_alone_do_not_count_as_agreement():
    """
    'road'/'india'/'shop' appear on countless pages — a real page that
    happens to contain them, while listing a different city entirely, is
    still a genuine mismatch.
    """
    gbp = "Shop 4, Chandani Chowk Road, Pune, Maharashtra 411001, India"
    page = "welcome to our showroom on the main road in india, find us in bandra west mumbai 400050"
    assert nap_check.address_mismatch(gbp, page) == gbp


def test_a_page_with_no_meaningful_tokens_is_never_a_mismatch():
    """
    Deliberate: if the page yielded nothing identifying (a JS-only shell, a
    stopword fragment), we have nothing to judge on, and silence beats a
    confident wrong claim.
    """
    gbp = "Shop 4, Chandani Chowk Road, Pune, Maharashtra 411001, India"
    assert nap_check.address_mismatch(gbp, "shop road india") is None


# ---------------------------------------------------------------------------
# The flaws themselves
# ---------------------------------------------------------------------------

_CLEAN_PARSED = {
    "meta_title": "t", "meta_description": "d", "h1_tags": ["h"],
    "has_cta": True, "has_contact": True, "has_testimonials": True,
    "has_blog": True, "homepage_text": "x " * 400, "has_canonical": True,
    "has_robots_noindex": False, "has_og_tags": True, "has_viewport_meta": True,
    "has_favicon": True, "has_booking_widget": True, "has_click_to_call": True,
    "has_menu_or_pricing": True,
}


def _flaws(**kwargs):
    base = dict(
        load_time_ms=1000, perf_score=90, seo_score=90, mobile_score=90,
        best_practices_score=90, has_ssl=True, parsed=_CLEAN_PARSED,
        has_structured_data=True, has_business_schema=True, readability_score=60,
        security_flaws=[], seo_page={}, accessibility_violations=[], broken_links=[],
    )
    base.update(kwargs)
    return WebsiteScraper._build_flaws(**base)


def test_phone_mismatch_becomes_a_high_severity_conversion_flaw():
    flaws = _flaws(nap_phone_mismatch="+91 98765 43210")
    matched = [f for f in flaws if "doesn't match the one on the Google Business listing" in f.description]
    assert len(matched) == 1
    assert matched[0].severity == "high"
    assert matched[0].category == "conversion"
    assert "+91 98765 43210" in matched[0].description


def test_address_mismatch_becomes_an_seo_flaw():
    flaws = _flaws(nap_address_mismatch="Indiranagar, Bengaluru 560038")
    matched = [f for f in flaws if "doesn't clearly appear on the website" in f.description]
    assert len(matched) == 1
    assert matched[0].category == "seo"


def test_no_nap_data_produces_no_nap_flaws():
    """A lead with no Google Business Profile behind it must be unaffected."""
    descriptions = " ".join(f.description for f in _flaws())
    assert "Google Business listing" not in descriptions
    assert "doesn't clearly appear on the website" not in descriptions


def test_an_expired_certificate_is_critical():
    flaws = _flaws(cert_expiry_days=-3)
    matched = [f for f in flaws if "expired 3 days ago" in f.description]
    assert len(matched) == 1
    assert matched[0].severity == "critical"


def test_a_certificate_expiring_within_two_weeks_is_high():
    flaws = _flaws(cert_expiry_days=5)
    matched = [f for f in flaws if "expires in 5 days" in f.description]
    assert len(matched) == 1
    assert matched[0].severity == "high"


def test_a_certificate_expiring_within_a_month_is_medium():
    flaws = _flaws(cert_expiry_days=25)
    matched = [f for f in flaws if "expires in 25 days" in f.description]
    assert len(matched) == 1
    assert matched[0].severity == "medium"


def test_a_healthy_certificate_produces_no_flaw():
    descriptions = " ".join(f.description for f in _flaws(cert_expiry_days=200))
    assert "certificate" not in descriptions.lower()


def test_an_unreadable_certificate_produces_no_flaw():
    """None means we couldn't check — that must never become a claim."""
    descriptions = " ".join(f.description for f in _flaws(cert_expiry_days=None))
    assert "certificate" not in descriptions.lower()


def test_expired_certificates_are_read_with_verification_relaxed():
    """
    An already-expired cert is the most valuable case this signal has, and a
    verifying TLS context refuses the handshake outright — so without the
    unverified retry the severe case would read identically to "couldn't
    check" and produce no flaw at all. Live-verified against
    expired.badssl.com (-4137 days) when this was added.
    """
    import inspect
    from analyzer import ssl_expiry

    source = inspect.getsource(ssl_expiry)
    assert "SSLCertVerificationError" in source, (
        "a verification failure must fall through to reading the dates anyway"
    )
    assert "verify=False" in source


def test_plain_http_is_skipped_without_a_handshake(monkeypatch):
    """No TLS to inspect, and no socket should be opened to find that out."""
    import asyncio
    from analyzer import ssl_expiry

    def _boom(*a, **k):
        raise AssertionError("should not open a connection for http://")

    monkeypatch.setattr(ssl_expiry, "_days_until_cert_expiry_blocking", _boom)
    assert asyncio.run(ssl_expiry.days_until_cert_expiry("http://example.com")) is None


def test_inapplicable_signals_are_not_recorded_as_degraded_coverage():
    """
    partial_coverage (any signal_status entry != "ok") bypasses
    should_contact()'s skip-if-healthy rule. Marking NAP as "no_data" on
    every lead that simply has no Google Business Profile — or SSL expiry on
    every plain-HTTP site — would force a contact decision on all of them
    regardless of score. "Not applicable" is not "we tried and failed".
    """
    import inspect
    from scrapers.website import WebsiteScraper

    source = inspect.getsource(WebsiteScraper.audit_website)
    assert 'if gbp_phone or gbp_address:' in source, (
        "NAP coverage must only be recorded when there was GBP data to compare against"
    )
    assert 'if has_ssl:' in source, (
        "SSL expiry coverage must only be recorded for sites actually served over HTTPS"
    )


# ---------------------------------------------------------------------------
# The two wiring bugs found while adding the above
# ---------------------------------------------------------------------------

def test_api_audit_passes_the_mobile_screenshot_to_the_ai():
    """
    The mobile viewport pass runs, costs real wall-clock time and feeds
    mobile axe-core/overflow flaws — but until 2026-08-09 /api/audit never
    handed the resulting IMAGE to the model, leaving the entire
    has_mobile_image prompt block dead on the path actually used.
    """
    import inspect
    import app

    source = inspect.getsource(app.audit_lead)
    assert "mobile_image_path" in source, (
        "/api/audit must pass the mobile screenshot through, or capturing it is wasted"
    )


def test_api_audit_passes_the_google_rating_to_the_ai():
    import inspect
    import app

    source = inspect.getsource(app.audit_lead)
    assert "rating=req.rating" in source


def test_a_non_numeric_rating_never_reaches_the_prompt():
    """
    The Playwright Maps fallback stores the literal string "N/A", which used
    to render as "GOOGLE BUSINESS RATING: N/A/5 stars from 0 reviews" — an
    unmeasured value presented as data.
    """
    from analyzer.ai_audit import AIAuditor
    from scrapers.website import WebsiteData

    web = WebsiteData(
        url="https://example.com", reachable=True, load_time_ms=1200,
        page_speed_score=55, seo_score=70, mobile_score=60,
        has_cta=True, has_contact=True, has_testimonials=True, has_blog=True,
        has_ssl=True, meta_title="Acme", meta_description="We do things",
    )

    assert "N/A/5" not in AIAuditor._build_prompt("Acme", None, web, rating="N/A")
    assert "GOOGLE BUSINESS RATING" not in AIAuditor._build_prompt("Acme", None, web, rating="N/A")
    # A real rating still comes through.
    assert "4.8" in AIAuditor._build_prompt("Acme", None, web, rating="4.8", reviews_count=120)
