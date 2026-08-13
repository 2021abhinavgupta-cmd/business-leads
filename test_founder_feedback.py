"""
Tests for the founder's three feedback points (added 2026-08-10):

  1. "How do we know they'll spend money on this?" — analyzer/budget_signal.py.
     A free, dashboard-only prioritisation heuristic built entirely from data
     already scraped (Google reviews/rating, detected ad/payment/booking
     tooling, Instagram followers). Deliberately NOT the founder's original
     idea (public P&L / paid PR-and-news search) — this lead population is
     essentially never publicly listed, and a paid search would cost real
     money per lead for near-zero coverage of a neighbourhood business that
     has simply never been written about. Confirmed as the direction to take
     before building it.
  2. "The email is lengthy" — EMAIL_VARIANT's default flipped classic -> short.
     The "short" variant (single most severe flaw, lower-friction ask) already
     existed as a measurable hypothesis; a live, specific length complaint is
     enough to default to it now rather than wait on data that was never
     being collected in the first place.
  3. "Too much jargon... sometimes the problem it identifies are wrong" — two
     new deterministic review_warnings checks, same belt-and-suspenders
     pattern as every other check in analyzer/ai_audit.py: a prevention
     instruction (the prompt already had a thorough "NO TECHNICAL JARGON"
     rule) can be ignored, so also detect it after the fact.

Unit tests only — no network, no API keys.
"""

import pytest

from analyzer.budget_signal import estimate_budget_fit


# ---------------------------------------------------------------------------
# 1. Budget-fit signal
# ---------------------------------------------------------------------------

def test_a_well_reviewed_highly_rated_business_reads_as_established():
    result = estimate_budget_fit(rating=4.8, reviews_count=850, technologies=[], ig_followers=0)
    assert result["tier"] == "established"


def test_no_data_at_all_reads_as_unclear_not_a_negative_judgement():
    """
    Unclear must not be presented as "can't afford it" — it's "we have no
    signal either way", which is the honest, common case for a brand-new
    business.
    """
    result = estimate_budget_fit(rating=None, reviews_count=None, technologies=[], ig_followers=None)
    assert result["tier"] == "unclear"
    assert "budget" not in result["label"].lower() or "not enough" in result["label"].lower()


def test_a_handful_of_reviews_alone_reads_as_growing_not_established():
    result = estimate_budget_fit(rating=5.0, reviews_count=15, technologies=[], ig_followers=0)
    assert result["tier"] in ("growing", "unclear")
    assert result["tier"] != "established"


def test_a_high_rating_from_too_few_reviews_is_not_scored():
    """One or two friends leaving 5 stars isn't a real signal."""
    with_few = estimate_budget_fit(rating=5.0, reviews_count=2, technologies=[], ig_followers=0)
    without_rating = estimate_budget_fit(rating=None, reviews_count=2, technologies=[], ig_followers=0)
    assert with_few["points"] == without_rating["points"]


@pytest.mark.parametrize("tech", ["Google Ads", "Meta Pixel", "HubSpot", "Mailchimp"])
def test_detected_ad_or_marketing_tooling_is_a_positive_signal(tech):
    baseline = estimate_budget_fit(rating=None, reviews_count=0, technologies=[], ig_followers=0)
    with_tech = estimate_budget_fit(rating=None, reviews_count=0, technologies=[tech], ig_followers=0)
    assert with_tech["points"] > baseline["points"]


@pytest.mark.parametrize("tech", ["Razorpay", "Stripe", "WooCommerce"])
def test_detected_payment_tooling_is_a_positive_signal(tech):
    baseline = estimate_budget_fit(rating=None, reviews_count=0, technologies=[], ig_followers=0)
    with_tech = estimate_budget_fit(rating=None, reviews_count=0, technologies=[tech], ig_followers=0)
    assert with_tech["points"] > baseline["points"]


def test_near_universal_analytics_tooling_is_not_scored():
    """
    Google Analytics/Tag Manager are on nearly every site in this lead
    population (verified against three real leads this session) — scoring
    them would add noise, not signal, since they don't discriminate.
    """
    baseline = estimate_budget_fit(rating=None, reviews_count=0, technologies=[], ig_followers=0)
    with_ga = estimate_budget_fit(
        rating=None, reviews_count=0,
        technologies=["Google Analytics", "Google Tag Manager"], ig_followers=0,
    )
    assert with_ga["points"] == baseline["points"]


def test_a_booking_widget_is_a_positive_signal():
    """A paid scheduling tool is a real recurring cost the business chose to take on."""
    baseline = estimate_budget_fit(rating=None, reviews_count=0, technologies=[], ig_followers=0)
    with_booking = estimate_budget_fit(
        rating=None, reviews_count=0, technologies=[], has_booking_widget=True, ig_followers=0,
    )
    assert with_booking["points"] > baseline["points"]


def test_a_large_instagram_following_is_a_positive_signal():
    small = estimate_budget_fit(rating=None, reviews_count=0, technologies=[], ig_followers=200)
    large = estimate_budget_fit(rating=None, reviews_count=0, technologies=[], ig_followers=20000)
    assert large["points"] > small["points"]


def test_malformed_inputs_do_not_raise():
    """Every field here originates from a scrape and can be missing/odd-shaped."""
    result = estimate_budget_fit(
        rating="N/A", reviews_count="not a number", technologies=None,
        has_booking_widget=None, ig_followers="???",
    )
    assert result["tier"] == "unclear"


def test_the_signal_explains_itself():
    """A bare tier with no reasoning is not useful for a human deciding priority."""
    result = estimate_budget_fit(rating=4.8, reviews_count=500, technologies=["Razorpay"], ig_followers=0)
    assert result["signals"]
    assert all(isinstance(s, str) for s in result["signals"])


def test_it_never_reaches_the_drafting_prompt_or_the_sent_copy():
    """
    This is a dashboard-only prioritisation heuristic. If it were readable by
    the drafting model or the sender, a wrong or presumptuous guess about a
    lead's finances could end up in copy the lead themselves reads.
    """
    import inspect

    from analyzer import ai_audit
    from emailer import base_sender

    assert "budget_signal" not in inspect.getsource(ai_audit)
    assert "budget_signal" not in inspect.getsource(base_sender)


# ---------------------------------------------------------------------------
# 2. Default variant flipped to "short"
# ---------------------------------------------------------------------------

def test_email_variant_defaults_to_short_now():
    import importlib

    import config

    importlib.reload(config)
    assert config.EMAIL_VARIANT == "short"


def test_classic_is_still_available_via_override(monkeypatch):
    import config

    monkeypatch.setattr(config, "EMAIL_VARIANT", "classic")
    assert config.EMAIL_VARIANT == "classic"


# ---------------------------------------------------------------------------
# 3a. Jargon detection
# ---------------------------------------------------------------------------

def _parsed(*paragraphs, subject="s", opening="o"):
    return {"email_subject": subject, "opening_line": opening,
            "flaws": [{"paragraph": p} for p in paragraphs]}


@pytest.mark.parametrize("jargon", [
    "Largest Contentful Paint takes 4.2s to render.",
    "Your site has a poor Cumulative Layout Shift score.",
    "ARIA roles are missing on several buttons.",
    "The site is missing structured data for search engines.",
])
def test_technical_jargon_is_flagged(jargon):
    from analyzer.ai_audit import AIAuditor

    warning = AIAuditor._check_jargon_words(_parsed(jargon), "Acme")
    assert warning is not None


def test_plain_language_copy_is_not_flagged():
    from analyzer.ai_audit import AIAuditor

    clean = _parsed("Your homepage takes about 4 seconds to fully load, which is slow for mobile visitors.")
    assert AIAuditor._check_jargon_words(clean, "Acme") is None


def test_the_jargon_terms_the_prompt_itself_warns_against_are_covered():
    """
    The prompt's own "NO TECHNICAL JARGON" instruction names these terms
    explicitly as ones to avoid — the deterministic check exists because a
    prevention instruction can be ignored, so it needs to know about the
    same terms the instruction does.
    """
    from analyzer.ai_audit import AIAuditor

    for term in ("largest contentful paint", "cumulative layout shift", "aria "):
        assert term in AIAuditor._JARGON_TERMS


# ---------------------------------------------------------------------------
# 3b. Email readability
# ---------------------------------------------------------------------------

def test_obscure_vocabulary_is_flagged_via_dale_chall(capsys):
    from analyzer.ai_audit import AIAuditor

    dense = _parsed(
        "The aggregation of substantively suboptimal architectural implementations "
        "across the enumerated infrastructural components collectively precipitates "
        "a materially deleterious impact upon prospective conversion methodologies "
        "and the concomitant acquisition of qualified consumer engagement pursuant "
        "to established industry benchmarks and comparative competitive positioning."
    )
    warning = AIAuditor._check_email_readability(dense, "Acme")
    capsys.readouterr()
    assert warning is not None
    assert "Dale-Chall" in warning


def test_a_single_jargon_sentence_buried_in_an_otherwise_plain_paragraph_is_still_caught():
    """
    The precision fix: a whole-paragraph average dilutes one bad sentence
    into invisibility. Scored per sentence, "Your canonical URL is missing
    hreflang tags." is caught even surrounded by ordinary prose that alone
    would pass cleanly.
    """
    from analyzer.ai_audit import AIAuditor

    mixed = _parsed(
        "I noticed your website takes about five seconds to load on mobile. "
        "Your canonical URL is missing hreflang tags. This confuses search "
        "crawlers and hurts how many people find you."
    )
    warning = AIAuditor._check_email_readability(mixed, "Acme")
    assert warning is not None
    assert "hreflang" in warning


def test_a_long_but_ordinary_vocabulary_sentence_is_flagged_via_flesch_not_dale_chall():
    """Sentence LENGTH/complexity is a separate axis from vocabulary familiarity."""
    from analyzer.ai_audit import AIAuditor

    long_but_plain = _parsed(
        "When someone who is looking for a place near their home visits your "
        "website on their phone after finding you through a search and then "
        "has to wait a long time before anything shows up on the screen and "
        "they are not sure if the page is even working at all they will often "
        "just give up and go back and click on one of the other results "
        "instead of waiting around for your site to finally finish loading "
        "everything it needs to show them."
    )
    warning = AIAuditor._check_email_readability(long_but_plain, "Acme")
    assert warning is not None
    assert "Flesch" in warning
    assert "Dale-Chall" not in warning


def test_plain_conversational_writing_is_not_flagged():
    from analyzer.ai_audit import AIAuditor

    plain = _parsed(
        "Your homepage takes a while to load on a phone. Most people won't wait "
        "that long, so they leave before they even see what you offer. That's a "
        "customer who never got the chance to book with you."
    )
    assert AIAuditor._check_email_readability(plain, "Acme") is None


def test_a_very_short_draft_is_not_scored():
    """Flesch scoring on a handful of words is noise, not signal."""
    from analyzer.ai_audit import AIAuditor

    short = _parsed("Your site is slow.")
    assert AIAuditor._check_email_readability(short, "Acme") is None


def test_both_new_checks_are_collected_into_review_warnings():
    import inspect

    from analyzer.ai_audit import AIAuditor

    source = inspect.getsource(AIAuditor.analyze_lead)
    assert "_check_jargon_words" in source
    assert "_check_email_readability" in source


# ---------------------------------------------------------------------------
# 1b. Dashboard wiring for the budget badge
# ---------------------------------------------------------------------------

def test_the_api_response_carries_the_budget_signal():
    import inspect

    import app as app_module

    source = inspect.getsource(app_module.audit_lead)
    assert '"budget_signal": estimate_budget_fit(' in source


def test_the_frontend_renders_the_badge_without_touching_the_email():
    jsx = open("frontend/src/App.jsx", encoding="utf-8").read()
    assert "budget_signal" in jsx
    assert "budget-badge" in jsx


def test_the_badge_is_styled_for_all_three_tiers():
    css = open("frontend/src/App.css", encoding="utf-8").read()
    for tier in ("established", "growing", "unclear"):
        assert f"budget-badge--{tier}" in css
