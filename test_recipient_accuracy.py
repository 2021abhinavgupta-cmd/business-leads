"""
Tests for recipient accuracy (added 2026-08-09).

Every other accuracy guard in this project protects what the email SAYS.
These protect who it goes TO, which matters more: a perfectly truthful email
sent to an address nobody owns is a hard bounce, and bounces damage sender
reputation for every future send. AWS reviews accounts at roughly a 5%
bounce rate — one bad address in a 20-send batch clears that on its own, and
that has already nearly happened here once (see CLAUDE.md, 2026-08-06).

Two strategies in find_decision_maker CONSTRUCT an address (from a person's
name, or from the marketing@ pattern) and cannot confirm the mailbox exists.
They used to return the identical dict shape as a genuinely scraped address,
so nothing downstream could tell "we found their email" from "we made one
up". Now every result carries is_guess and domain_accepts_mail.

Also covers the self-consistency warning finally reaching review_warnings.

Unit tests only — no network, no DNS, no API keys.
"""

import pytest

from analyzer.ai_audit import AIAuditor
from enrichment.decision_maker import DecisionMaker


def _dm() -> DecisionMaker:
    """A DecisionMaker without running __init__ (no httpx client, no API key)."""
    return DecisionMaker.__new__(DecisionMaker)


# ---------------------------------------------------------------------------
# is_guess — can this address be trusted?
# ---------------------------------------------------------------------------

def test_the_generic_fallback_address_is_marked_as_a_guess(monkeypatch):
    monkeypatch.setattr(DecisionMaker, "domain_accepts_mail", staticmethod(lambda e: True))
    result = _dm()._fallback_patterns("example.com", "Acme Team")

    assert result["email"] == "marketing@example.com"
    assert result["is_guess"] is True, "an invented address must never look like a found one"


def test_a_scraped_address_is_not_marked_as_a_guess(monkeypatch):
    monkeypatch.setattr(DecisionMaker, "domain_accepts_mail", staticmethod(lambda e: True))
    result = _dm()._finalise("Acme Team", "hello@example.com", "", is_guess=False)

    assert result["is_guess"] is False


def test_every_result_carries_both_trust_fields(monkeypatch):
    """A new strategy must not be able to omit these by forgetting."""
    monkeypatch.setattr(DecisionMaker, "domain_accepts_mail", staticmethod(lambda e: True))
    result = _dm()._finalise("Acme Team", "hello@example.com", "", is_guess=False)

    assert "is_guess" in result
    assert "domain_accepts_mail" in result


# ---------------------------------------------------------------------------
# _guess_email_from_name — the dead "verify" half is gone
# ---------------------------------------------------------------------------

def test_a_name_becomes_a_plausible_address():
    assert _dm()._guess_email_from_name("Kshitij Gupta", "example.com") == "kshitij.gupta@example.com"


def test_a_single_name_uses_the_bare_first_name():
    assert _dm()._guess_email_from_name("Kshitij", "example.com") == "kshitij@example.com"


def test_an_unusable_name_produces_nothing_rather_than_a_junk_address():
    assert _dm()._guess_email_from_name("", "example.com") == ""
    assert _dm()._guess_email_from_name("!!!", "example.com") == ""


def test_the_bogus_smtp_catch_all_detection_is_gone():
    """
    The old _guess_and_verify_email tried to detect a catch-all domain by
    checking whether an obviously fake address validated — but
    email-validator's check_deliverability resolves DNS/MX only and never
    opens an SMTP connection, so the fake address validated on every domain
    with mail. is_catch_all was therefore always True, the pattern loop
    never ran, and the function always returned patterns[0] regardless.
    Verified empirically against gmail.com and mmga.agency before removal.
    """
    import inspect
    from enrichment import decision_maker

    source = inspect.getsource(decision_maker)
    assert "bounce-test-992384" not in source, "the always-true catch-all probe must stay removed"
    assert not hasattr(DecisionMaker, "_guess_and_verify_email"), (
        "renamed to _guess_email_from_name — it guesses and cannot verify"
    )


# ---------------------------------------------------------------------------
# domain_accepts_mail — tri-state on purpose
# ---------------------------------------------------------------------------

def test_a_lookup_failure_is_unknown_not_undeliverable(monkeypatch):
    """
    DNS from a cloud host is unreliable enough that treating a timeout as
    "undeliverable" would block real leads — the same trap already documented
    for scraped-address validation.
    """
    from enrichment import decision_maker

    def _boom(*a, **k):
        raise RuntimeError("DNS timeout")

    monkeypatch.setattr(decision_maker, "validate_email", _boom)
    assert DecisionMaker.domain_accepts_mail("someone@example.com") is None


def test_a_definitively_dead_domain_is_false(monkeypatch):
    from enrichment import decision_maker
    from email_validator import EmailUndeliverableError

    def _undeliverable(*a, **k):
        raise EmailUndeliverableError("The domain name does not exist.")

    monkeypatch.setattr(decision_maker, "validate_email", _undeliverable)
    assert DecisionMaker.domain_accepts_mail("someone@nope-99823.com") is False


def test_a_resolvable_domain_is_true(monkeypatch):
    from enrichment import decision_maker

    monkeypatch.setattr(decision_maker, "validate_email", lambda *a, **k: True)
    assert DecisionMaker.domain_accepts_mail("someone@example.com") is True


def test_a_missing_address_is_unknown():
    assert DecisionMaker.domain_accepts_mail("") is None
    assert DecisionMaker.domain_accepts_mail("not-an-address") is None


# ---------------------------------------------------------------------------
# The warnings actually reach the send gate
# ---------------------------------------------------------------------------

def _audit_client(monkeypatch, dm_result):
    from fastapi.testclient import TestClient
    import app as app_module

    async def _fake_screenshot(*a, **k):
        return ("shot.jpg", "<html></html>", {})

    async def _fake_audit_website(*a, **k):
        return _real_web_data()

    monkeypatch.setattr(app_module.config, "API_KEY", None)
    monkeypatch.setattr(app_module.ses, "check_quota", lambda: {"Max24HourSend": 100, "SentLast24Hours": 0})
    monkeypatch.setattr(app_module, "generate_audit_screenshot", _fake_screenshot)
    monkeypatch.setattr(app_module.web_scraper, "audit_website", _fake_audit_website)
    monkeypatch.setattr(app_module.auditor, "analyze_lead", lambda *a, **k: {"flaws": [], "review_warnings": [], "overall_score": 40})
    monkeypatch.setattr(app_module.decision_maker, "find_decision_maker", lambda *a, **k: dm_result)
    monkeypatch.setattr(app_module.ses, "generate_email", lambda *a, **k: ("subject", "body"))
    monkeypatch.setattr(app_module.db, "log_cost", lambda *a, **k: None)
    monkeypatch.setattr(app_module.db, "log_draft", lambda *a, **k: None)

    return TestClient(app_module.app, raise_server_exceptions=False)


def _real_web_data():
    """A real WebsiteData — _build_prompt reads far more than a stub provides."""
    from scrapers.website import WebsiteData

    return WebsiteData(
        url="https://example.com", reachable=True, load_time_ms=1200,
        page_speed_score=55, seo_score=70, mobile_score=60,
        has_cta=True, has_contact=True, has_testimonials=True, has_blog=True,
        has_ssl=True, meta_title="Acme", meta_description="We do things",
    )


def test_a_guessed_address_produces_a_review_warning(monkeypatch):
    client = _audit_client(monkeypatch, {
        "name": "Acme Team", "email": "marketing@example.com", "title": "",
        "is_guess": True, "domain_accepts_mail": True,
    })
    res = client.post("/api/audit", json={"company": "Acme", "website": "https://example.com", "force": True})

    assert res.status_code == 200, res.text
    warnings = res.json().get("review_warnings") or []
    assert any("GUESSED" in w for w in warnings), warnings


def test_a_dead_domain_produces_a_bounce_warning(monkeypatch):
    client = _audit_client(monkeypatch, {
        "name": "Acme Team", "email": "marketing@nope-99823.com", "title": "",
        "is_guess": True, "domain_accepts_mail": False,
    })
    res = client.post("/api/audit", json={"company": "Acme", "website": "https://example.com", "force": True})

    assert res.status_code == 200, res.text
    warnings = res.json().get("review_warnings") or []
    assert any("hard bounce" in w for w in warnings), warnings


def test_a_real_scraped_address_adds_no_warning(monkeypatch):
    client = _audit_client(monkeypatch, {
        "name": "Acme Team", "email": "hello@example.com", "title": "",
        "is_guess": False, "domain_accepts_mail": True,
    })
    res = client.post("/api/audit", json={"company": "Acme", "website": "https://example.com", "force": True})

    assert res.status_code == 200, res.text
    assert (res.json().get("review_warnings") or []) == []


# ---------------------------------------------------------------------------
# Self-consistency finally reaching review_warnings
# ---------------------------------------------------------------------------

def test_total_disagreement_returns_a_warning(capsys):
    first = {"flaws": [{"paragraph": "a", "source_quote": "[CRITICAL] LCP takes 4.2s"}]}
    second = {"flaws": [{"paragraph": "b", "source_quote": "[HIGH] something else entirely"}]}

    warning = AIAuditor._apply_self_consistency(first, second, "Acme")
    capsys.readouterr()

    assert warning is not None
    assert "NO findings in common" in warning
    assert len(first["flaws"]) == 1, "an empty email is still worse than one needing review"


def test_partial_disagreement_returns_a_warning(capsys):
    first = {"flaws": [
        {"paragraph": "a", "source_quote": "[CRITICAL] LCP takes 4.2s"},
        {"paragraph": "b", "source_quote": "[LOW] no favicon"},
    ]}
    second = {"flaws": [{"paragraph": "c", "source_quote": "[CRITICAL] LCP takes 4.2s"}]}

    warning = AIAuditor._apply_self_consistency(first, second, "Acme")
    capsys.readouterr()

    assert warning is not None
    assert "Dropped 1 of 2" in warning
    assert len(first["flaws"]) == 1, "the claim only one sample made must be dropped"


def test_full_agreement_returns_no_warning(capsys):
    quote = "[CRITICAL] LCP takes 4.2s"
    first = {"flaws": [{"paragraph": "a", "source_quote": quote}]}
    second = {"flaws": [{"paragraph": "b", "source_quote": quote}]}

    assert AIAuditor._apply_self_consistency(first, second, "Acme") is None
    capsys.readouterr()


def test_the_consistency_warning_is_collected_into_review_warnings():
    import inspect

    source = inspect.getsource(AIAuditor.analyze_lead)
    assert "consistency_warning" in source
    assert source.index("consistency_warning = self._apply_self_consistency") < source.index("review_warnings = [w for w in ("), (
        "the warning must be produced before the list that collects it"
    )


# ---------------------------------------------------------------------------
# Cache honesty + configurable threshold
# ---------------------------------------------------------------------------

def test_a_replayed_audit_is_labelled_as_cached():
    import inspect
    import app

    source = inspect.getsource(app.audit_lead)
    assert '"cached": True' in source, (
        "a replayed result with no marker is indistinguishable from a live measurement"
    )
    assert "if not req.force:" in source, "a deliberate re-audit must be able to bypass the cache"


def test_contact_threshold_is_configurable():
    import config
    from analyzer.ai_audit import CONTACT_THRESHOLD

    assert CONTACT_THRESHOLD == config.CONTACT_THRESHOLD
    assert config.CONTACT_THRESHOLD == 70, "default must be unchanged"
