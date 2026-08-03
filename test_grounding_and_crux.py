"""
Tests for the three accuracy additions from the 2026-08-01 pass:
real-user CrUX field data, source-quote grounding, and self-consistency.

Unit tests only — no network, no API keys. The CrUX payloads below are real
responses captured live from nesinternational.org.
"""

import pytest

from analyzer.crux import extract_crux_from_pagespeed, _metrics_to_vitals
from analyzer.ai_audit import AIAuditor
from scrapers.website import WebsiteScraper


# ---------------------------------------------------------------------------
# CrUX extraction
# ---------------------------------------------------------------------------

# Trimmed from a real PageSpeed Insights response.
_REAL_PAGESPEED_RESPONSE = {
    "loadingExperience": {
        "metrics": {
            "LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 2785, "category": "AVERAGE"},
            "CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": 1, "category": "FAST"},
            "INTERACTION_TO_NEXT_PAINT": {"percentile": 102, "category": "FAST"},
            "EXPERIMENTAL_TIME_TO_FIRST_BYTE": {"percentile": 1430, "category": "AVERAGE"},
        }
    }
}


def test_extracts_real_user_vitals_from_pagespeed_response():
    vitals = extract_crux_from_pagespeed(_REAL_PAGESPEED_RESPONSE)
    assert vitals["lcp_ms"] == 2785
    assert vitals["inp_ms"] == 102
    assert vitals["ttfb_ms"] == 1430


def test_cls_is_converted_from_crux_integer_scale_to_decimal():
    """CrUX reports CLS x100; every threshold in this codebase uses decimals."""
    vitals = extract_crux_from_pagespeed(_REAL_PAGESPEED_RESPONSE)
    assert vitals["cls"] == 0.01, "a p75 of 1 means 0.01, not 1.0 (which would be a critical flaw)"


def test_falls_back_to_origin_level_data_when_page_level_is_absent():
    payload = {"originLoadingExperience": {"metrics": {"LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 3100}}}}
    assert extract_crux_from_pagespeed(payload)["lcp_ms"] == 3100


def test_no_crux_data_returns_empty_not_zero():
    """A site below Google's traffic threshold has no data — that must not read as 0ms."""
    assert extract_crux_from_pagespeed({"lighthouseResult": {}}) == {}
    assert extract_crux_from_pagespeed({}) == {}


def test_crux_api_metric_shape_is_parsed():
    """
    The dedicated CrUX API returns CLS as a decimal STRING ("0.01"), while
    PageSpeed's loadingExperience returns the same value as an integer at
    100x ("1"). Live-verified on jns.ac.in. Treating one like the other is
    silent: the value either vanishes or lands 100x off, and 0.01 vs 1.0 is
    the difference between fine and a critical flaw.
    """
    metrics = {
        "largest_contentful_paint": {"percentiles": {"p75": 1818}},
        "cumulative_layout_shift": {"percentiles": {"p75": "0.01"}},
        "interaction_to_next_paint": {"percentiles": {"p75": 128}},
    }
    vitals = _metrics_to_vitals(metrics)
    assert vitals == {"lcp_ms": 1818, "cls": 0.01, "inp_ms": 128}


def test_the_two_sources_agree_on_the_same_underlying_cls():
    """Both formats must land on the identical decimal value."""
    from_api = _metrics_to_vitals({"cumulative_layout_shift": {"percentiles": {"p75": "0.01"}}})
    from_pagespeed = extract_crux_from_pagespeed(
        {"loadingExperience": {"metrics": {"CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": 1}}}}
    )
    assert from_api["cls"] == from_pagespeed["cls"] == 0.01


def test_unparseable_metric_is_dropped_not_stored_as_none():
    """
    A None left in the dict would masquerade as "CrUX answered" and block
    the fallback to measured/lab vitals — this is exactly how CLS came back
    as None before the string form was handled.
    """
    vitals = _metrics_to_vitals({"cumulative_layout_shift": {"percentiles": {"p75": "not-a-number"}}})
    assert "cls" not in vitals


# ---------------------------------------------------------------------------
# Vitals priority: CrUX > real measured > lab
# ---------------------------------------------------------------------------

def test_inp_flaw_uses_plain_language_and_real_user_framing():
    flaws = WebsiteScraper._build_flaws(
        load_time_ms=1000, perf_score=90, seo_score=90, mobile_score=90,
        best_practices_score=90, inp_ms=650,
        has_ssl=True,
        parsed={"meta_title": "t", "meta_description": "d", "h1_tags": ["h"],
                "has_cta": True, "has_contact": True, "has_testimonials": True,
                "has_blog": True, "homepage_text": "x " * 400, "has_canonical": True,
                "has_robots_noindex": False, "has_og_tags": True, "has_viewport_meta": True,
                "has_favicon": True, "has_booking_widget": True, "has_click_to_call": True,
                "has_menu_or_pricing": True},
        has_structured_data=True, has_business_schema=True, readability_score=60,
        security_flaws=[], seo_page={}, accessibility_violations=[], broken_links=[],
    )
    inp_flaws = [f for f in flaws if "tap or click" in f.description]
    assert len(inp_flaws) == 1
    assert "650ms" in inp_flaws[0].description
    assert inp_flaws[0].severity == "high"
    # No raw metric jargon in copy a business owner reads.
    assert "INP" not in inp_flaws[0].description


def test_good_inp_is_not_flagged():
    flaws = WebsiteScraper._build_flaws(
        load_time_ms=1000, perf_score=90, seo_score=90, mobile_score=90,
        best_practices_score=90, inp_ms=102,
        has_ssl=True,
        parsed={"meta_title": "t", "meta_description": "d", "h1_tags": ["h"],
                "has_cta": True, "has_contact": True, "has_testimonials": True,
                "has_blog": True, "homepage_text": "x " * 400, "has_canonical": True,
                "has_robots_noindex": False, "has_og_tags": True, "has_viewport_meta": True,
                "has_favicon": True, "has_booking_widget": True, "has_click_to_call": True,
                "has_menu_or_pricing": True},
        has_structured_data=True, has_business_schema=True, readability_score=60,
        security_flaws=[], seo_page={}, accessibility_violations=[], broken_links=[],
    )
    assert not [f for f in flaws if "tap or click" in f.description]


# ---------------------------------------------------------------------------
# Source-quote grounding — deterministic, no LLM involved
# ---------------------------------------------------------------------------

_PROMPT = (
    "FLAWS DETECTED (ranked most severe first):\n"
    "  - [CRITICAL] Largest Contentful Paint takes 4.2s to render\n"
    "  - [HIGH] 12 accessibility violations found across the pages we checked\n"
)


def test_fabricated_source_quote_is_flagged(capsys):
    parsed = {"flaws": [{"paragraph": "Your checkout is broken.",
                         "source_quote": "[CRITICAL] Checkout form does not submit"}]}
    AIAuditor._verify_source_quotes(parsed, _PROMPT, "Acme")
    assert "does NOT appear in the audit data" in capsys.readouterr().out


def test_real_source_quote_passes_silently(capsys):
    parsed = {"flaws": [{"paragraph": "Your main image is slow.",
                         "source_quote": "[CRITICAL] Largest Contentful Paint takes 4.2s to render"}]}
    AIAuditor._verify_source_quotes(parsed, _PROMPT, "Acme")
    assert capsys.readouterr().out == ""


def test_whitespace_and_case_differences_are_tolerated(capsys):
    parsed = {"flaws": [{"paragraph": "p",
                         "source_quote": "largest contentful    paint TAKES 4.2s to render"}]}
    AIAuditor._verify_source_quotes(parsed, _PROMPT, "Acme")
    assert capsys.readouterr().out == "", "trivial reformatting must not count as fabrication"


def test_missing_source_quote_is_flagged(capsys):
    parsed = {"flaws": [{"paragraph": "Some claim with no citation at all."}]}
    AIAuditor._verify_source_quotes(parsed, _PROMPT, "Acme")
    assert "no source quote given" in capsys.readouterr().out


def test_no_flaws_is_a_noop(capsys):
    AIAuditor._verify_source_quotes({"flaws": []}, _PROMPT, "Acme")
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# Self-consistency
# ---------------------------------------------------------------------------

def test_claims_in_only_one_sample_are_dropped(capsys):
    first = {"flaws": [
        {"paragraph": "a", "source_quote": "[CRITICAL] LCP takes 4.2s"},
        {"paragraph": "b", "source_quote": "[HIGH] 12 accessibility violations"},
    ]}
    second = {"flaws": [{"paragraph": "differently worded", "source_quote": "[CRITICAL] LCP takes 4.2s"}]}

    AIAuditor._apply_self_consistency(first, second, "Acme")

    assert len(first["flaws"]) == 1
    assert first["flaws"][0]["source_quote"] == "[CRITICAL] LCP takes 4.2s"
    assert "dropped 1 of 2" in capsys.readouterr().out


def test_full_agreement_keeps_everything(capsys):
    quotes = ["[CRITICAL] LCP takes 4.2s", "[HIGH] 12 accessibility violations"]
    first = {"flaws": [{"paragraph": f"p{i}", "source_quote": q} for i, q in enumerate(quotes)]}
    second = {"flaws": [{"paragraph": f"other{i}", "source_quote": q} for i, q in enumerate(quotes)]}

    AIAuditor._apply_self_consistency(first, second, "Acme")

    assert len(first["flaws"]) == 2
    assert "dropped" not in capsys.readouterr().out


def test_total_disagreement_keeps_the_original_rather_than_emptying_the_email(capsys):
    first = {"flaws": [{"paragraph": "a", "source_quote": "[CRITICAL] LCP takes 4.2s"}]}
    second = {"flaws": [{"paragraph": "b", "source_quote": "[HIGH] something else entirely"}]}

    AIAuditor._apply_self_consistency(first, second, "Acme")

    assert len(first["flaws"]) == 1, "an empty email is worse than one needing review"
    assert "entirely different findings" in capsys.readouterr().out


def test_second_sample_without_quotes_is_ignored():
    first = {"flaws": [{"paragraph": "a", "source_quote": "[CRITICAL] LCP takes 4.2s"}]}
    AIAuditor._apply_self_consistency(first, {"flaws": []}, "Acme")
    assert len(first["flaws"]) == 1


# ---------------------------------------------------------------------------
# Schema wiring
# ---------------------------------------------------------------------------

def test_openai_strict_schema_is_derived_not_hardcoded():
    """
    OpenAI strict mode rejects undeclared properties, so a hardcoded copy of
    the flaw shape silently makes it the one provider that can't return a
    newly added field.
    """
    import inspect
    source = inspect.getsource(AIAuditor._call_openai)
    assert '"properties": {"paragraph"' not in source, (
        "the flaw schema must be derived from _RESPONSE_JSON_SCHEMA, not restated"
    )


def test_shared_schema_requires_a_source_quote():
    from analyzer.ai_audit import _RESPONSE_JSON_SCHEMA
    flaw_schema = _RESPONSE_JSON_SCHEMA["properties"]["flaws"]["items"]
    assert "source_quote" in flaw_schema["properties"]
    assert "source_quote" in flaw_schema["required"]
