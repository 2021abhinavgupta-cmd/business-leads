"""
Tests for email CONTENT quality (added 2026-08-09).

Everything else in this suite guards whether the email is true. These guard
whether it's any good — and, more importantly, whether the project can ever
find out. Engagement data (email_opens, email_replies) had been collected
since 2026-08-06, but every email was structurally identical and nothing
recorded what was tried, so an outcome could never be attributed to a
decision and copy could only be argued about.

Also covers a real false claim found in the follow-up copy: it asked whether
the recipient had seen "the mobile website screenshot I attached" when the
attachment is always the desktop capture.

Unit tests only — no network, no API keys.
"""

import importlib

import pytest

import config
from emailer.base_sender import BaseSender


def _sender() -> BaseSender:
    """A BaseSender without a transport — only the copy generators are used."""
    return BaseSender.__new__(BaseSender)


_ANALYSIS = {
    "email_subject": "Quick question about Acme",
    "opening_line": "Loved what you're doing with cosmetic dentistry in Pune.",
    "flaws": [
        {"paragraph": "Your homepage takes 4.2 seconds to show anything.", "source_quote": "a"},
        {"paragraph": "Three of your nav links go nowhere.", "source_quote": "b"},
        {"paragraph": "There's no way to book online.", "source_quote": "c"},
    ],
}


# ---------------------------------------------------------------------------
# The follow-up false claims
# ---------------------------------------------------------------------------

def test_followups_never_claim_a_mobile_screenshot_was_attached():
    """
    /api/send attaches make_screenshot_filename(...) -> "_audit.jpg", the
    DESKTOP capture. The mobile screenshot ("_mobile.jpg") is only ever fed
    to the AI as a second vision input and is never attached to anything, so
    the old "the mobile website screenshot I attached" was false on every
    single follow-up ever sent.
    """
    for stage in (1, 2):
        body = _sender().generate_followup("Priya", stage, "Kshitij").lower()
        assert "mobile" not in body, f"stage {stage} claims something about mobile it cannot know"


def test_followups_do_not_assume_the_flaws_were_ui_problems():
    """
    generate_followup receives only a name and a stage — it has no access to
    the original audit. Flaws are just as often performance, SEO, security,
    certificate expiry, broken links or a NAP mismatch, so "these UI issues"
    was wrong whenever the original email wasn't about design.
    """
    for stage in (1, 2):
        body = _sender().generate_followup("Priya", stage, "Kshitij").lower()
        assert "ui issue" not in body
        assert "your mobile site" not in body


def test_followups_still_reference_the_original_email_generically():
    """Vague is fine; wrong is not. It should still feel like a follow-up."""
    body = _sender().generate_followup("Priya", 1, "Kshitij").lower()
    assert "sent" in body or "follow" in body or "bumping" in body


def test_followups_are_signed():
    for stage in (1, 2):
        assert "Kshitij" in _sender().generate_followup("Priya", stage, "Kshitij")


# ---------------------------------------------------------------------------
# Salutation — the mail-merge tell
# ---------------------------------------------------------------------------

def test_a_real_person_name_is_used_in_the_greeting(monkeypatch):
    monkeypatch.setattr(config, "EMAIL_VARIANT", "classic")
    _, body = _sender().generate_email("Acme Dental", "Priya", _ANALYSIS, "Kshitij")
    assert body.startswith("Hi Priya,")


def test_the_generic_company_team_fallback_is_not_used_in_the_greeting(monkeypatch):
    """
    "Hi Acme Dental Team," is a visible mail-merge tell, and this fallback is
    the COMMON case — contact discovery usually can't find a real person.
    """
    monkeypatch.setattr(config, "EMAIL_VARIANT", "classic")
    _, body = _sender().generate_email("Acme Dental", "Acme Dental Team", _ANALYSIS, "Kshitij")
    assert body.startswith("Hi there,")
    assert "Acme Dental Team" not in body


def test_a_bare_team_fallback_is_also_caught():
    assert BaseSender._is_a_real_person_name("Team", "Acme") is False
    assert BaseSender._is_a_real_person_name("", "Acme") is False
    assert BaseSender._is_a_real_person_name("Acme Team", "Acme") is False
    assert BaseSender._is_a_real_person_name("Priya", "Acme") is True


# ---------------------------------------------------------------------------
# Variants
# ---------------------------------------------------------------------------

def test_classic_variant_sends_every_flaw(monkeypatch):
    monkeypatch.setattr(config, "EMAIL_VARIANT", "classic")
    _, body = _sender().generate_email("Acme", "Priya", _ANALYSIS, "Kshitij")

    for flaw in _ANALYSIS["flaws"]:
        assert flaw["paragraph"] in body


def test_short_variant_sends_only_the_most_severe_flaw(monkeypatch):
    """The AI ranks worst-first, so [0] is the strongest single card."""
    monkeypatch.setattr(config, "EMAIL_VARIANT", "short")
    _, body = _sender().generate_email("Acme", "Priya", _ANALYSIS, "Kshitij")

    assert _ANALYSIS["flaws"][0]["paragraph"] in body
    assert _ANALYSIS["flaws"][1]["paragraph"] not in body
    assert _ANALYSIS["flaws"][2]["paragraph"] not in body


def test_short_variant_is_materially_shorter(monkeypatch):
    monkeypatch.setattr(config, "EMAIL_VARIANT", "classic")
    _, classic = _sender().generate_email("Acme", "Priya", _ANALYSIS, "Kshitij")
    monkeypatch.setattr(config, "EMAIL_VARIANT", "short")
    _, short = _sender().generate_email("Acme", "Priya", _ANALYSIS, "Kshitij")

    assert len(short.split()) < len(classic.split())


def test_short_variant_uses_a_lower_friction_ask(monkeypatch):
    """A meeting request is a high-commitment ask on a first cold touch."""
    monkeypatch.setattr(config, "EMAIL_VARIANT", "short")
    _, body = _sender().generate_email("Acme", "Priya", _ANALYSIS, "Kshitij")

    assert "10 minute call" not in body
    assert "No call needed" in body


def test_classic_variant_is_unchanged_by_default(monkeypatch):
    """The default must not silently alter what was being sent before."""
    monkeypatch.setattr(config, "EMAIL_VARIANT", "classic")
    monkeypatch.setattr(config, "SOCIAL_PROOF_LINE", "")
    _, body = _sender().generate_email("Acme", "Priya", _ANALYSIS, "Kshitij")

    assert "I've been helping brands fix exactly these things." in body
    assert "Worth a quick 10 minute call this week?" in body


def test_a_configured_social_proof_line_replaces_the_generic_claim(monkeypatch):
    monkeypatch.setattr(config, "EMAIL_VARIANT", "classic")
    monkeypatch.setattr(config, "SOCIAL_PROOF_LINE", "I did this for two clinics in Pune last month.")
    _, body = _sender().generate_email("Acme", "Priya", _ANALYSIS, "Kshitij")

    assert "two clinics in Pune" in body
    assert "I've been helping brands fix exactly these things." not in body


def test_the_default_variant_is_classic():
    importlib.reload(config)
    assert config.EMAIL_VARIANT == "classic"


# ---------------------------------------------------------------------------
# Variant performance — the point of recording it
# ---------------------------------------------------------------------------

def _seed(tmp_path, monkeypatch, rows):
    from storage import db

    monkeypatch.setattr(db, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.sqlite"))
    db.init_db()
    for company, variant, body in rows:
        db.log_email(company, "https://x.com", f"{company}@x.com", "me@x.com", "s", body, message_id=f"<{company}@x>", variant=variant)
    return db


def test_word_count_is_derived_from_the_body_actually_stored(tmp_path, monkeypatch):
    db = _seed(tmp_path, monkeypatch, [("a", "short", "one two three four five")])
    assert db.get_email_history()[0]["body_word_count"] == 5


def test_variant_is_recorded_on_every_send(tmp_path, monkeypatch):
    db = _seed(tmp_path, monkeypatch, [("a", "short", "body")])
    assert db.get_email_history()[0]["variant"] == "short"


def test_performance_groups_by_variant(tmp_path, monkeypatch):
    db = _seed(tmp_path, monkeypatch, [
        ("a", "short", "body"), ("b", "short", "body"), ("c", "classic", "body"),
    ])
    perf = {row["variant"]: row for row in db.get_variant_performance()}

    assert perf["short"]["sent"] == 2
    assert perf["classic"]["sent"] == 1


def test_a_small_sample_is_flagged_as_not_enough_data(tmp_path, monkeypatch):
    """
    One reply on four sends reads as 25%, which is noise. The row must say so
    rather than presenting a number that looks like a result.
    """
    db = _seed(tmp_path, monkeypatch, [("a", "short", "body")])
    row = db.get_variant_performance()[0]

    assert row["enough_data"] is False


def test_sends_predating_variant_tracking_are_bucketed_not_dropped(tmp_path, monkeypatch):
    db = _seed(tmp_path, monkeypatch, [("a", "", "body")])
    assert db.get_variant_performance()[0]["variant"] == "(unrecorded)"


# ---------------------------------------------------------------------------
# Prompt hygiene — a worked example is the strongest signal in a prompt, so
# an example that breaks the prompt's own rules teaches the model to break
# them. Added 2026-08-09.
# ---------------------------------------------------------------------------

def test_the_red_box_example_does_not_claim_accessibility_hurts_seo():
    """
    The old example ended "...invisible to screen readers, which is hurting
    your SEO" — an accessibility problem asserted as an SEO one. That exact
    unsupported leap produced a real false claim on a live lead.
    """
    from analyzer.ai_audit import AIAuditor
    from scrapers.website import WebsiteData

    web = WebsiteData(
        url="https://example.com", reachable=True, load_time_ms=1200,
        page_speed_score=55, seo_score=70, mobile_score=60,
        has_cta=True, has_contact=True, has_testimonials=True, has_blog=True,
        has_ssl=True, meta_title="Acme", meta_description="We do things",
    )
    prompt = AIAuditor._build_prompt("Acme", None, web)

    assert "which is hurting your SEO" not in prompt
    assert "do NOT claim an accessibility problem affects SEO" in prompt


def test_prompt_examples_do_not_contain_the_dashes_they_forbid():
    """
    The prompt says "NEVER use hyphens (-) or dashes (—) anywhere in your
    response", then demonstrated an em dash inside a quoted example of
    desired output.
    """
    import re
    from analyzer import ai_audit

    source = open(ai_audit.__file__, encoding="utf-8").read()
    examples = re.findall(r"\(e\.g\., '([^']+)'", source)
    assert examples, "expected to find worked examples in the prompt"
    for example in examples:
        assert "\u2014" not in example, f"example demonstrates a forbidden em dash: {example[:70]}"


def test_forbidden_dashes_are_detected_in_generated_copy(capsys):
    from analyzer.ai_audit import AIAuditor

    parsed = {"email_subject": "s", "opening_line": "o",
              "flaws": [{"paragraph": "Your site is slow \u2014 that costs you customers."}]}
    warning = AIAuditor._check_forbidden_dashes(parsed, "Acme")
    capsys.readouterr()
    assert warning is not None
    assert "\u2014" in warning


def test_clean_copy_produces_no_dash_warning(capsys):
    from analyzer.ai_audit import AIAuditor

    parsed = {"email_subject": "s", "opening_line": "o",
              "flaws": [{"paragraph": "Your site is slow and that costs you customers."}]}
    assert AIAuditor._check_forbidden_dashes(parsed, "Acme") is None
    capsys.readouterr()


def test_the_dash_check_is_collected_into_review_warnings():
    import inspect
    from analyzer.ai_audit import AIAuditor

    source = inspect.getsource(AIAuditor.analyze_lead)
    assert "_check_forbidden_dashes" in source
