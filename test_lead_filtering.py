"""
Tests for the established-business filters (2026-08-31).

Two independent gates, both aimed at "only spend a draft and a send on a
business that is actually established":

1. MIN_GOOGLE_REVIEWS — scrapers/google_maps.py drops a Maps lead whose
   listing reports a REAL review count below the floor, before it costs
   anything downstream. A listing with no review data at all (0, blank,
   "N/A") is kept: no data is "unknown", not "small".

2. MIN_BUDGET_TIER — after the site audit, analyzer/budget_signal.py's
   tier ("unclear"/"growing"/"established") gates whether the AI draft
   runs at all. "growing" skips only "unclear" leads.

Unit tests only — no network.
"""

import pytest

import config
from analyzer.budget_signal import clears_min_tier
from scrapers.google_maps import _passes_review_floor


# ---------------------------------------------------------------------------
# MIN_GOOGLE_REVIEWS — the scrape-time review floor
# ---------------------------------------------------------------------------

@pytest.fixture
def floor_2000(monkeypatch):
    monkeypatch.setattr(config, "MIN_GOOGLE_REVIEWS", 2000)


def test_a_lead_below_the_floor_is_dropped(floor_2000):
    assert _passes_review_floor({"Reviews Count": 40}) is False
    assert _passes_review_floor({"Reviews Count": 1999}) is False


def test_a_lead_at_or_above_the_floor_passes(floor_2000):
    assert _passes_review_floor({"Reviews Count": 2000}) is True
    assert _passes_review_floor({"Reviews Count": 8123}) is True


def test_no_review_data_is_kept_not_dropped(floor_2000):
    """
    The 'keep unknowns' rule. A brand-new listing, the free Playwright/OSINT
    fallback (always 0), and a non-Maps source with no review field must all
    pass — absence of review data is not evidence of a small business.
    """
    assert _passes_review_floor({"Reviews Count": 0}) is True
    assert _passes_review_floor({"Reviews Count": ""}) is True
    assert _passes_review_floor({"Reviews Count": "N/A"}) is True
    assert _passes_review_floor({}) is True
    assert _passes_review_floor({"Reviews Count": None}) is True


def test_a_floor_of_zero_disables_the_filter(monkeypatch):
    monkeypatch.setattr(config, "MIN_GOOGLE_REVIEWS", 0)
    assert _passes_review_floor({"Reviews Count": 1}) is True


def test_a_string_count_that_is_numeric_is_still_compared(floor_2000):
    assert _passes_review_floor({"Reviews Count": "50"}) is False
    assert _passes_review_floor({"Reviews Count": "5000"}) is True


def test_the_api_scraper_filters_its_result_list(monkeypatch):
    """
    _scrape_via_api applies the floor before dedupe/limit. Stub the HTTP call
    so this stays offline.
    """
    from scrapers.google_maps import GoogleMapsScraper

    monkeypatch.setattr(config, "MIN_GOOGLE_REVIEWS", 2000)
    scraper = GoogleMapsScraper()
    scraper.api_key = "x"

    page = {
        "places": [
            {"displayName": {"text": "Big Chain Gym"}, "websiteUri": "https://big.example",
             "rating": 4.6, "userRatingCount": 5400},
            {"displayName": {"text": "Corner Studio"}, "websiteUri": "https://corner.example",
             "rating": 4.9, "userRatingCount": 32},
            {"displayName": {"text": "New Place"}, "websiteUri": "https://new.example",
             "userRatingCount": 0},
        ]
    }

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return page

    monkeypatch.setattr(scraper.client, "post", lambda *a, **k: _Resp())
    monkeypatch.setattr("scrapers.google_maps.time.sleep", lambda *_a: None)

    leads = scraper._scrape_via_api("gym", "Mumbai", limit=10)
    names = {lead["Company"] for lead in leads}
    assert "Big Chain Gym" in names       # 5400 reviews
    assert "New Place" in names           # no review data -> kept
    assert "Corner Studio" not in names   # 32 reviews -> dropped


# ---------------------------------------------------------------------------
# MIN_BUDGET_TIER — the audit-time budget gate
# ---------------------------------------------------------------------------

def test_growing_gate_skips_only_unclear_leads():
    assert clears_min_tier({"tier": "unclear"}, "growing") is False
    assert clears_min_tier({"tier": "growing"}, "growing") is True
    assert clears_min_tier({"tier": "established"}, "growing") is True


def test_established_gate_skips_unclear_and_growing():
    assert clears_min_tier({"tier": "unclear"}, "established") is False
    assert clears_min_tier({"tier": "growing"}, "established") is False
    assert clears_min_tier({"tier": "established"}, "established") is True


def test_an_empty_or_unclear_minimum_disables_the_gate():
    assert clears_min_tier({"tier": "unclear"}, "") is True
    assert clears_min_tier({"tier": "unclear"}, "unclear") is True
    assert clears_min_tier({"tier": "unclear"}, "   ") is True
    assert clears_min_tier({"tier": "unclear"}, "nonsense") is True


def test_a_missing_or_malformed_budget_signal_is_treated_as_unclear():
    """
    Only runs after a completed audit, so "no recognised tier" means the
    signals were checked and nothing was found — a real skip at the growing
    gate, not an accident.
    """
    assert clears_min_tier(None, "growing") is False
    assert clears_min_tier({}, "growing") is False
    assert clears_min_tier({"tier": "???"}, "growing") is False


def test_case_and_whitespace_are_normalised():
    assert clears_min_tier({"tier": "GROWING"}, " Growing ") is True
    assert clears_min_tier({"tier": "Unclear"}, "GROWING") is False


# ---------------------------------------------------------------------------
# Wiring — the gate runs before the AI draft, on both entry points
# ---------------------------------------------------------------------------

def test_api_audit_gates_before_calling_the_ai():
    import inspect
    import app as app_module

    source = inspect.getsource(app_module.audit_lead)
    gate_at = source.index("clears_min_tier(budget_signal, config.MIN_BUDGET_TIER)")
    ai_at = source.index("auditor.analyze_lead")
    assert gate_at < ai_at, "the budget gate must short-circuit before the paid AI call"
    assert "below_min_budget_tier" in source


def test_main_batch_runner_gates_before_calling_the_ai():
    import inspect
    import main as main_module

    source = inspect.getsource(main_module.process_single_lead)
    gate_at = source.index("clears_min_tier(budget_signal, config.MIN_BUDGET_TIER)")
    ai_at = source.index("auditor.analyze_lead")
    assert gate_at < ai_at
    assert '"skipped_low_budget"' in source


def test_the_gate_reuses_the_one_budget_signal_it_also_returns():
    """No second estimate_budget_fit call — computed once, gated on, returned."""
    import inspect
    import app as app_module

    source = inspect.getsource(app_module.audit_lead)
    assert source.count("estimate_budget_fit(") == 1
    assert '"budget_signal": budget_signal,' in source


def test_defaults_are_the_gentle_active_setting():
    """
    The user asked for this filter on by default. growing skips only
    zero-signal leads; the review floor is set high on purpose.
    """
    assert config.MIN_BUDGET_TIER == "growing"
    assert config.MIN_GOOGLE_REVIEWS == 2000
