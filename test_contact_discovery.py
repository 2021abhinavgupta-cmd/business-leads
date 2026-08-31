"""
Tests for the free contact-discovery improvements (2026-08-31).

Two additions, both $0 and both about getting a REAL address rather than a
pattern guess:

1. `_extract_emails_from_html` — decodes Cloudflare's data-cfemail / email-
   protection obfuscation, reads mailto: targets, and normalises the
   bracketed "x [at] y [dot] z" form, none of which a plain regex over
   visible text sees. A large share of these leads sit behind Cloudflare
   with their contact address encoded in the HTML.

2. SMTP mailbox verification (config.VERIFY_EMAIL_SMTP, default off) — after
   LinkedIn yields a NAME (it never yields an email), the tool probes the
   domain's own mail server for each likely pattern and only returns one as
   a verified address if the server confirms it exists AND the domain is not
   catch-all. Every other outcome (catch-all, greylisted, blocked, no MX)
   leaves the address exactly as trusted as the old pure-guess path did.

Every network call is stubbed. No real SMTP, no real DNS.
"""

import pytest

import config
from enrichment import decision_maker as dm
from enrichment.decision_maker import (
    DecisionMaker,
    _decode_cf_email,
    _deobfuscate,
    _extract_emails_from_html,
    _person_email_patterns,
    _verify_mailbox,
)


@pytest.fixture(autouse=True)
def _reset_smtp_breaker():
    """The circuit breaker is module-level state; each test starts clean."""
    dm._smtp_consecutive_failures = 0
    dm._smtp_breaker_tripped = False
    yield
    dm._smtp_consecutive_failures = 0
    dm._smtp_breaker_tripped = False


def _cf_encode(address: str, key: int = 0x2a) -> str:
    """Produce a data-cfemail hex blob for *address* the way Cloudflare does."""
    return format(key, "02x") + "".join(format(ord(c) ^ key, "02x") for c in address)


# ---------------------------------------------------------------------------
# Cloudflare decode
# ---------------------------------------------------------------------------

def test_cloudflare_encoded_address_round_trips():
    assert _decode_cf_email(_cf_encode("owner@yogahouse.in")) == "owner@yogahouse.in"


def test_a_malformed_cf_blob_returns_empty_not_garbage():
    assert _decode_cf_email("") == ""
    assert _decode_cf_email("zz") == ""
    assert _decode_cf_email("7") == ""


def test_a_cloudflare_protected_address_is_extracted_from_the_attribute():
    html = f'<a class="__cf_email__" data-cfemail="{_cf_encode("hi@acme.co")}">[email&#160;protected]</a>'
    assert "hi@acme.co" in _extract_emails_from_html(html)


def test_a_cloudflare_protected_address_is_extracted_from_an_href():
    html = f'<a href="/cdn-cgi/l/email-protection#{_cf_encode("hi@acme.co")}">Email</a>'
    assert "hi@acme.co" in _extract_emails_from_html(html)


# ---------------------------------------------------------------------------
# mailto: and plain extraction
# ---------------------------------------------------------------------------

def test_mailto_link_is_read_and_query_string_stripped():
    html = '<a href="mailto:studio@yogahouse.in?subject=Booking%20enquiry">Contact</a>'
    assert _extract_emails_from_html(html) == {"studio@yogahouse.in"}


def test_a_comma_separated_mailto_yields_every_address():
    html = '<a href="mailto:a@x.com,b@x.com">mail us</a>'
    assert _extract_emails_from_html(html) == {"a@x.com", "b@x.com"}


def test_a_plain_address_in_text_is_still_found():
    assert "plain@x.com" in _extract_emails_from_html("<p>write to plain@x.com</p>")


def test_empty_or_missing_html_is_handled():
    assert _extract_emails_from_html("") == set()
    assert _extract_emails_from_html(None) == set()


# ---------------------------------------------------------------------------
# De-obfuscation — the bracketed form only, on purpose
# ---------------------------------------------------------------------------

def test_the_bracketed_obfuscation_form_is_normalised():
    assert "jane@acme.co" in _extract_emails_from_html("jane [at] acme [dot] co")
    assert "jane@acme.co" in _extract_emails_from_html("jane(at)acme(dot)co")


def test_the_bare_word_form_is_deliberately_left_alone():
    """
    " at " and " dot " appear constantly in ordinary prose. Turning them into
    @ and . manufactures addresses that were never on the page, and a
    fabricated recipient is the worst error this module can make.
    """
    text = "our team is great at design and lives in the dot com world"
    assert _deobfuscate(text) == text
    assert _extract_emails_from_html(text) == set()


def test_a_prose_sentence_with_the_word_at_does_not_become_an_address():
    html = "<p>Reach the founder, who is brilliant at branding, on LinkedIn.</p>"
    assert _extract_emails_from_html(html) == set()


# ---------------------------------------------------------------------------
# Name -> pattern candidates
# ---------------------------------------------------------------------------

def test_patterns_are_ordered_most_likely_first():
    patterns = _person_email_patterns("Jane Doe", "acme.co")
    assert patterns[0] == "jane.doe@acme.co"
    assert "janedoe@acme.co" in patterns
    assert "jdoe@acme.co" in patterns


def test_a_single_name_only_produces_the_first_name_pattern():
    assert _person_email_patterns("Madonna", "acme.co") == ["madonna@acme.co"]


def test_punctuation_and_middle_names_are_handled():
    patterns = _person_email_patterns("Jane Q. O'Brien", "acme.co")
    assert patterns[0] == "jane.obrien@acme.co"


def test_an_empty_name_produces_nothing():
    assert _person_email_patterns("", "acme.co") == []


# ---------------------------------------------------------------------------
# _verify_mailbox — the tri-state, all network stubbed
# ---------------------------------------------------------------------------

def _stub_smtp(monkeypatch, mx=("mx.acme.co",), probe=None):
    monkeypatch.setattr(dm, "_mx_hosts", lambda domain: list(mx))
    monkeypatch.setattr(dm, "_smtp_probe", probe)


def test_a_confirmed_mailbox_on_a_non_catchall_domain_is_valid(monkeypatch):
    codes = {"real@acme.co": 250}
    _stub_smtp(monkeypatch, probe=lambda host, rcpt: codes.get(rcpt, 550))
    assert _verify_mailbox("real@acme.co") == "valid"


def test_a_domain_that_accepts_everything_is_catch_all_not_valid(monkeypatch):
    _stub_smtp(monkeypatch, probe=lambda host, rcpt: 250)  # accepts the sentinel too
    assert _verify_mailbox("anything@acme.co") == "catch_all"


def test_a_hard_rejection_is_invalid(monkeypatch):
    _stub_smtp(monkeypatch, probe=lambda host, rcpt: 550)
    assert _verify_mailbox("nope@acme.co") == "invalid"


def test_greylisting_is_inconclusive_never_a_rejection(monkeypatch):
    _stub_smtp(monkeypatch, probe=lambda host, rcpt: 451)
    assert _verify_mailbox("maybe@acme.co") == "inconclusive"


def test_a_domain_with_no_mx_is_inconclusive(monkeypatch):
    monkeypatch.setattr(dm, "_mx_hosts", lambda domain: [])
    assert _verify_mailbox("x@nomx.co") == "inconclusive"


def test_a_dropped_connection_is_inconclusive(monkeypatch):
    _stub_smtp(monkeypatch, probe=lambda host, rcpt: None)
    assert _verify_mailbox("x@acme.co") == "inconclusive"


def test_repeated_blocks_trip_the_circuit_breaker(monkeypatch):
    _stub_smtp(monkeypatch, probe=lambda host, rcpt: None)
    for _ in range(dm._SMTP_MAX_CONSECUTIVE_FAILURES):
        _verify_mailbox("x@acme.co")
    assert dm._smtp_breaker_tripped
    # And once tripped, it stops probing entirely.
    calls = []
    monkeypatch.setattr(dm, "_smtp_probe", lambda host, rcpt: calls.append(rcpt) or 250)
    assert _verify_mailbox("y@acme.co") == "inconclusive"
    assert calls == []


def test_one_success_resets_the_failure_streak(monkeypatch):
    state = {"n": 0}

    def _probe(host, rcpt):
        state["n"] += 1
        if state["n"] == 1:
            return None            # one block
        if rcpt.startswith("nx-"):
            return 550             # sentinel rejected -> not catch-all
        return 250                 # real address accepted

    _stub_smtp(monkeypatch, probe=_probe)
    assert _verify_mailbox("a@acme.co") == "inconclusive"
    assert _verify_mailbox("b@acme.co") == "valid"
    assert dm._smtp_consecutive_failures == 0


# ---------------------------------------------------------------------------
# find_decision_maker wiring
# ---------------------------------------------------------------------------

@pytest.fixture
def maker(monkeypatch):
    """A DecisionMaker with every discovery strategy neutralised except the
    one under test."""
    m = DecisionMaker()
    monkeypatch.setattr(m, "_scrape_website_for_email", lambda *a, **k: "")
    monkeypatch.setattr(m, "_find_email_via_osint", lambda *a, **k: "")
    monkeypatch.setattr(m, "_find_via_claude_web_fetch", lambda *a, **k: {})
    monkeypatch.setattr(dm.DecisionMaker, "domain_accepts_mail", staticmethod(lambda email: True))
    return m


def test_linkedin_name_plus_a_verified_pattern_is_not_a_guess(monkeypatch, maker):
    monkeypatch.setattr(config, "VERIFY_EMAIL_SMTP", True)
    monkeypatch.setattr(maker, "_find_ceo_name", lambda company: "Priya Sharma")
    monkeypatch.setattr(
        dm, "_verify_mailbox",
        lambda email: "valid" if email == "priya.sharma@acme.co" else "invalid",
    )

    result = maker.find_decision_maker("Acme", "https://acme.co")
    assert result["email"] == "priya.sharma@acme.co"
    assert result["is_guess"] is False


def test_linkedin_name_with_nothing_verifiable_falls_back_to_the_flagged_guess(monkeypatch, maker):
    monkeypatch.setattr(config, "VERIFY_EMAIL_SMTP", True)
    monkeypatch.setattr(maker, "_find_ceo_name", lambda company: "Priya Sharma")
    monkeypatch.setattr(dm, "_verify_mailbox", lambda email: "catch_all")

    result = maker.find_decision_maker("Acme", "https://acme.co")
    assert result["email"] == "priya.sharma@acme.co"  # the single best guess
    assert result["is_guess"] is True                  # still needs a human


def test_verification_off_never_opens_an_smtp_conversation(monkeypatch, maker):
    monkeypatch.setattr(config, "VERIFY_EMAIL_SMTP", False)
    monkeypatch.setattr(maker, "_find_ceo_name", lambda company: "Priya Sharma")
    monkeypatch.setattr(dm, "_verify_mailbox", lambda email: pytest.fail("must not probe when off"))

    result = maker.find_decision_maker("Acme", "https://acme.co")
    assert result["is_guess"] is True


def test_a_verified_role_address_replaces_the_marketing_at_fallback(monkeypatch, maker):
    monkeypatch.setattr(config, "VERIFY_EMAIL_SMTP", True)
    monkeypatch.setattr(maker, "_find_ceo_name", lambda company: "")  # no person
    monkeypatch.setattr(
        dm, "_verify_mailbox",
        lambda email: "valid" if email == "hello@acme.co" else "invalid",
    )

    result = maker.find_decision_maker("Acme", "https://acme.co")
    assert result["email"] == "hello@acme.co"
    assert result["is_guess"] is False


def test_the_fallback_is_still_an_unverified_guess_when_no_role_address_exists(monkeypatch, maker):
    monkeypatch.setattr(config, "VERIFY_EMAIL_SMTP", True)
    monkeypatch.setattr(maker, "_find_ceo_name", lambda company: "")
    monkeypatch.setattr(dm, "_verify_mailbox", lambda email: "invalid")

    result = maker.find_decision_maker("Acme", "https://acme.co")
    assert result["email"] == "marketing@acme.co"
    assert result["is_guess"] is True


def test_a_scraped_cloudflare_address_is_used_directly_and_not_a_guess(monkeypatch):
    """End to end through _scrape_website_for_email with real HTML, no network."""
    m = DecisionMaker()
    monkeypatch.setattr(dm.DecisionMaker, "domain_accepts_mail", staticmethod(lambda email: True))
    monkeypatch.setattr(m, "_find_ceo_name", lambda company: pytest.fail("should not reach strategy 2"))

    html = f'<a class="__cf_email__" data-cfemail="{_cf_encode("studio@acme.co")}">x</a>'
    result = m.find_decision_maker("Acme", "https://acme.co", html_content=html)
    assert result["email"] == "studio@acme.co"
    assert result["is_guess"] is False
