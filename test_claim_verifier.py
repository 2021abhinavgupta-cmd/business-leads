"""
Tests for analyzer/claim_verifier.py (added 2026-08-10).

The verifier exists because every other accuracy check in this pipeline reads
the same audit data the drafting model read. _verify_grounding is handed the
prompt text and asked whether the draft follows from it, so a wrong number in
the prompt is confirmed as faithfully reproduced and the email goes out. Two
models agreeing about bad input is not verification.

This one re-measures from the live site instead, which is the only way to
catch a claim that was already wrong before the AI saw it.

The behaviour these tests pin down hardest is the SILENCE. A verifier that
warns when it simply could not reach the site would flag good drafts on every
network hiccup, and this codebase's recurring bug is exactly that: a missing
signal reported as a finding.

Unit tests only — every network call is stubbed.
"""

import pytest

import config
from analyzer import claim_verifier


def _draft(*paragraphs, subject="Quick question", opening="Nice work."):
    return {
        "email_subject": subject,
        "opening_line": opening,
        "flaws": [{"paragraph": p} for p in paragraphs],
    }


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(config, "VERIFY_CLAIMS_LIVE", True)


# ---------------------------------------------------------------------------
# Claim extraction
# ---------------------------------------------------------------------------

def test_claims_are_read_from_every_part_the_recipient_sees():
    text = claim_verifier._draft_text(
        _draft("body one", "body two", subject="subj", opening="open")
    )
    for fragment in ("subj", "open", "body one", "body two"):
        assert fragment in text


def test_phones_in_copy_are_normalised_for_comparison():
    found = claim_verifier._phones_in("call +91 86559 30022 or 022 2640 1234")
    assert claim_verifier.normalise_phone("+91 86559 30022") in found
    assert len(found) == 2


def test_years_and_prices_are_not_read_as_phone_numbers():
    assert claim_verifier._phones_in("since 2019, from 500 to 2000 rupees") == set()


# ---------------------------------------------------------------------------
# Phone claims
# ---------------------------------------------------------------------------

def test_a_number_present_on_the_live_page_raises_no_warning(monkeypatch):
    monkeypatch.setattr(claim_verifier, "fetch_page_text",
                        lambda url: "Call us on +91 86559 30022 today")
    parsed = _draft("Your listed number +91 86559 30022 does not match.")
    assert claim_verifier._check_phone_claims(parsed, "https://x.in", "") is None


def test_a_differently_formatted_but_identical_number_raises_no_warning(monkeypatch):
    """This is the real bug: same number, two spellings, reported as a mismatch."""
    monkeypatch.setattr(claim_verifier, "fetch_page_text",
                        lambda url: "phone: +918655930022")
    parsed = _draft("We saw +91 86559 30022 on your site.")
    assert claim_verifier._check_phone_claims(parsed, "https://x.in", "") is None


def test_a_number_found_nowhere_is_flagged(monkeypatch):
    monkeypatch.setattr(claim_verifier, "fetch_page_text",
                        lambda url: "Call us on +91 86559 30022 today")
    parsed = _draft("Customers are calling +91 99999 11111 and getting nowhere.")
    warning = claim_verifier._check_phone_claims(parsed, "https://x.in", "")
    assert warning is not None
    assert "Phone claim unverified" in warning


def test_the_google_listed_number_counts_as_known(monkeypatch):
    """
    A draft quoting the GBP number while telling them the site shows a
    different one is making a legitimate claim, not an unverifiable one.
    """
    monkeypatch.setattr(claim_verifier, "fetch_page_text",
                        lambda url: "phone: +91 22222 33333")
    parsed = _draft("Google lists +91 86559 30022 for you.")
    assert claim_verifier._check_phone_claims(parsed, "https://x.in", "+91 86559 30022") is None


def test_an_unreachable_page_produces_no_warning(monkeypatch):
    """Could-not-check must never read as could-not-verify."""
    monkeypatch.setattr(claim_verifier, "fetch_page_text", lambda url: None)
    parsed = _draft("Your number +91 99999 11111 is wrong.")
    assert claim_verifier._check_phone_claims(parsed, "https://x.in", "") is None


def test_a_page_with_no_numbers_produces_no_warning(monkeypatch):
    """The number may be in an image or a JS widget. Absence proves nothing."""
    monkeypatch.setattr(claim_verifier, "fetch_page_text", lambda url: "no digits here")
    parsed = _draft("Your number +91 99999 11111 is wrong.")
    assert claim_verifier._check_phone_claims(parsed, "https://x.in", "") is None


def test_a_draft_quoting_no_numbers_is_skipped(monkeypatch):
    def _explode(url):
        raise AssertionError("should not fetch when there is nothing to check")

    monkeypatch.setattr(claim_verifier, "fetch_page_text", _explode)
    assert claim_verifier._check_phone_claims(_draft("Your site is slow."), "https://x.in", "") is None


# ---------------------------------------------------------------------------
# Speed claims
# ---------------------------------------------------------------------------

def _fixed_lcp(mobile, desktop):
    def _lookup(url, strategy="mobile"):
        return mobile if strategy == "mobile" else desktop

    return _lookup


def test_a_claim_matching_the_desktop_measurement_is_accepted(monkeypatch):
    monkeypatch.setattr(claim_verifier, "pagespeed_lcp_seconds", _fixed_lcp(15.9, 4.1))
    parsed = _draft("Your homepage takes about 5 seconds to appear.")
    assert claim_verifier._check_speed_claim(parsed, "https://x.in") is None


def test_a_claim_matching_the_mobile_measurement_is_accepted(monkeypatch):
    monkeypatch.setattr(claim_verifier, "pagespeed_lcp_seconds", _fixed_lcp(15.9, 4.1))
    parsed = _draft("Your homepage takes about 16 seconds on a phone.")
    assert claim_verifier._check_speed_claim(parsed, "https://x.in") is None


def test_a_claim_matching_neither_measurement_is_flagged(monkeypatch):
    monkeypatch.setattr(claim_verifier, "pagespeed_lcp_seconds", _fixed_lcp(15.9, 4.1))
    parsed = _draft("Your homepage takes 40 seconds to load.")
    warning = claim_verifier._check_speed_claim(parsed, "https://x.in")
    assert warning is not None
    assert "Speed claim unverified" in warning


def test_small_differences_are_tolerated(monkeypatch):
    """Page speed varies run to run; this is not a rounding police."""
    monkeypatch.setattr(claim_verifier, "pagespeed_lcp_seconds", _fixed_lcp(15.9, 4.1))
    parsed = _draft("Your homepage takes 4.5 seconds.")
    assert claim_verifier._check_speed_claim(parsed, "https://x.in") is None


def test_no_measurement_available_produces_no_warning(monkeypatch):
    monkeypatch.setattr(claim_verifier, "pagespeed_lcp_seconds", lambda url, strategy="mobile": None)
    parsed = _draft("Your homepage takes 40 seconds to load.")
    assert claim_verifier._check_speed_claim(parsed, "https://x.in") is None


def test_durations_outside_a_believable_range_are_ignored(monkeypatch):
    """"10 minute call" and similar copy must not be read as a speed claim."""
    monkeypatch.setattr(claim_verifier, "pagespeed_lcp_seconds", _fixed_lcp(15.9, 4.1))
    parsed = _draft("Worth a quick call? It takes 0.1 seconds to say yes.")
    assert claim_verifier._check_speed_claim(parsed, "https://x.in") is None


# ---------------------------------------------------------------------------
# Broken-link claims
# ---------------------------------------------------------------------------

_PAGE = """
<html><body>
  <a href="#content">Skip to content</a>
  <a href="/courses/">Courses</a>
  <a href="https://facebook.com/acme">Facebook</a>
  <a href="/about/">About</a>
</body></html>
"""


class _Resp:
    def __init__(self, text, url="https://x.in/", status_code=200):
        self.text = text
        self.url = url
        self.status_code = status_code


def test_a_count_claimed_against_an_all_healthy_page_is_flagged(monkeypatch):
    monkeypatch.setattr(claim_verifier.httpx, "get", lambda *a, **k: _Resp(_PAGE))
    monkeypatch.setattr(claim_verifier, "probe_url", lambda url: True)
    parsed = _draft("Your site has 9 broken links scattered across pages.")
    warning = claim_verifier._check_broken_link_claim(parsed, "https://x.in/")
    assert warning is not None
    assert "Broken-link claim unverified" in warning


def test_a_genuinely_dead_link_suppresses_the_warning(monkeypatch):
    monkeypatch.setattr(claim_verifier.httpx, "get", lambda *a, **k: _Resp(_PAGE))
    monkeypatch.setattr(claim_verifier, "probe_url", lambda url: False)
    parsed = _draft("Your site has 9 broken links.")
    assert claim_verifier._check_broken_link_claim(parsed, "https://x.in/") is None


def test_fragments_and_bot_blocking_hosts_are_not_probed(monkeypatch):
    """The two false-positive sources that produced the original bad claim."""
    probed = []
    monkeypatch.setattr(claim_verifier.httpx, "get", lambda *a, **k: _Resp(_PAGE))
    monkeypatch.setattr(claim_verifier, "probe_url", lambda url: probed.append(url) or True)
    claim_verifier._check_broken_link_claim(_draft("2 broken links"), "https://x.in/")

    assert not any("#content" in u for u in probed)
    assert not any("facebook.com" in u for u in probed)
    assert any("/courses/" in u for u in probed)


def test_mostly_inconclusive_probes_produce_no_warning(monkeypatch):
    monkeypatch.setattr(claim_verifier.httpx, "get", lambda *a, **k: _Resp(_PAGE))
    monkeypatch.setattr(claim_verifier, "probe_url", lambda url: None)
    parsed = _draft("Your site has 9 broken links.")
    assert claim_verifier._check_broken_link_claim(parsed, "https://x.in/") is None


def test_a_draft_with_no_link_count_is_skipped(monkeypatch):
    def _explode(*a, **k):
        raise AssertionError("should not fetch when there is nothing to check")

    monkeypatch.setattr(claim_verifier.httpx, "get", _explode)
    assert claim_verifier._check_broken_link_claim(_draft("Your site is slow."), "https://x.in/") is None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def test_verification_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(config, "VERIFY_CLAIMS_LIVE", False)

    def _explode(*a, **k):
        raise AssertionError("should not run when disabled")

    monkeypatch.setattr(claim_verifier, "fetch_page_text", _explode)
    assert claim_verifier.verify_claims(_draft("+91 99999 11111 is wrong"), "https://x.in") == []


def test_a_missing_url_is_skipped():
    assert claim_verifier.verify_claims(_draft("anything"), "") == []


def test_a_failing_check_cannot_break_the_audit(monkeypatch):
    """
    This runs inside analyze_lead. A verifier that raises would cost the lead
    entirely, which is a far worse outcome than an unverified claim.
    """
    def _explode(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(claim_verifier, "_check_phone_claims", _explode)
    monkeypatch.setattr(claim_verifier, "_check_broken_link_claim", lambda *a, **k: None)
    monkeypatch.setattr(claim_verifier, "_check_speed_claim", lambda *a, **k: None)
    assert claim_verifier.verify_claims(_draft("anything"), "https://x.in") == []


def test_warnings_from_every_check_are_collected(monkeypatch):
    monkeypatch.setattr(claim_verifier, "_check_phone_claims", lambda *a, **k: "phone warning")
    monkeypatch.setattr(claim_verifier, "_check_broken_link_claim", lambda *a, **k: "link warning")
    monkeypatch.setattr(claim_verifier, "_check_speed_claim", lambda *a, **k: "speed warning")
    assert claim_verifier.verify_claims(_draft("x"), "https://x.in") == [
        "phone warning", "link warning", "speed warning",
    ]


def test_the_verifier_is_wired_into_review_warnings():
    """Warnings have to reach the red banner and the /api/send 409 gate."""
    import inspect

    from analyzer.ai_audit import AIAuditor

    source = inspect.getsource(AIAuditor.analyze_lead)
    assert "claim_verifier.verify_claims" in source


# ---------------------------------------------------------------------------
# Tri-state probing
# ---------------------------------------------------------------------------

def test_a_transport_error_is_unknown_not_dead(monkeypatch):
    """
    Collapsing "we could not reach it" into False is how a working link gets
    described to a business owner as broken.
    """
    class _Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): raise RuntimeError("dns failure")
        def __exit__(self, *a): return False

    monkeypatch.setattr(claim_verifier.httpx, "Client", _Client)
    assert claim_verifier.probe_url("https://x.in/a") is None
