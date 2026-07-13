"""
Unit tests for WebsiteScraper._build_flaws — no network, no API keys required.
Locks in the flaw-reconciliation behavior (severity mapping, ranking, dedup
inputs) added to replace the old raw-dump-everything-into-the-prompt approach.
"""

from scrapers.website import WebsiteScraper

_BASE_PARSED = {
    "meta_title": "A Title",
    "meta_description": "A description",
    "h1_tags": ["A heading"],
    "has_cta": True,
    "has_contact": True,
    "has_testimonials": True,
    "has_blog": True,
    "has_canonical": True,
    "is_noindexed": False,
    "has_og_tags": True,
    "has_viewport_meta": True,
    "has_favicon": True,
    "has_click_to_call": True,
    "has_whatsapp_link": True,
    "has_booking_widget": True,
    "has_menu_or_pricing": True,
}

_BASE_KWARGS = dict(
    load_time_ms=1000,
    perf_score=90,
    seo_score=90,
    mobile_score=90,
    best_practices_score=90,
    has_ssl=True,
    parsed=_BASE_PARSED,
    has_structured_data=True,
    has_business_schema=True,
    readability_score=70.0,
    security_flaws=[],
    seo_page={},
    accessibility_violations=[],
    broken_links=[],
)


def _build(**overrides):
    kwargs = {**_BASE_KWARGS, **overrides}
    return WebsiteScraper._build_flaws(**kwargs)


def test_clean_site_has_no_flaws():
    assert _build() == []


def test_missing_ssl_is_critical():
    flaws = _build(has_ssl=False)
    assert any(f.severity == "critical" and "HTTPS" in f.description for f in flaws)


def test_noindexed_is_critical():
    flaws = _build(parsed={**_BASE_PARSED, "is_noindexed": True})
    assert any(f.severity == "critical" and "noindex" in f.description for f in flaws)


def test_axe_violation_severity_mapping():
    violations = [
        {"impact": "critical", "help": "Critical issue", "nodes_count": 1, "pages": ["/"]},
        {"impact": "serious", "help": "Serious issue", "nodes_count": 2, "pages": ["/"]},
        {"impact": "moderate", "help": "Moderate issue", "nodes_count": 3, "pages": ["/"]},
        {"impact": "minor", "help": "Minor issue", "nodes_count": 4, "pages": ["/"]},
    ]
    flaws = _build(accessibility_violations=violations)
    by_desc = {f.description: f.severity for f in flaws}
    assert by_desc["[CRITICAL] Critical issue (1 instance(s))"] == "critical"
    assert by_desc["[SERIOUS] Serious issue (2 instance(s))"] == "high"
    assert by_desc["[MODERATE] Moderate issue (3 instance(s))"] == "medium"
    assert by_desc["[MINOR] Minor issue (4 instance(s))"] == "low"


def test_axe_violation_consolidated_across_pages_shows_page_list():
    violation = {"impact": "serious", "help": "Links must have discernible text", "nodes_count": 2, "pages": ["/", "/about-us", "mobile view"]}
    flaws = _build(accessibility_violations=[violation])
    matches = [f for f in flaws if "Links must have discernible text" in f.description]
    assert len(matches) == 1
    assert "across /, /about-us, mobile view" in matches[0].description


def test_broken_links_aggregate_to_one_flaw():
    broken = [{"url": f"https://x.com/{i}", "type": "link", "status": 404} for i in range(3)]
    flaws = _build(broken_links=broken)
    broken_flaws = [f for f in flaws if "broken link" in f.description]
    assert len(broken_flaws) == 1
    assert "3 broken" in broken_flaws[0].description


def test_flaws_are_ranked_most_severe_first():
    flaws = _build(has_ssl=False, parsed={**_BASE_PARSED, "has_testimonials": False})
    severities = [f.severity for f in flaws]
    assert severities == sorted(severities, key=lambda s: {"critical": 0, "high": 1, "medium": 2, "low": 3}[s])


def test_thin_content_flag():
    flaws = _build(seo_page={"word_count": 50})
    assert any("thin content" in f.description for f in flaws)


def test_missing_viewport_meta_flagged():
    flaws = _build(parsed={**_BASE_PARSED, "has_viewport_meta": False})
    assert any(f.severity == "high" and "viewport" in f.description for f in flaws)


def test_missing_favicon_flagged():
    flaws = _build(parsed={**_BASE_PARSED, "has_favicon": False})
    assert any("favicon" in f.description for f in flaws)


def test_too_many_fonts_flagged():
    flaws = _build(font_families=["Arial", "Georgia", "Comic Sans MS", "Verdana", "Times New Roman"])
    assert any("different fonts" in f.description for f in flaws)


def test_few_fonts_not_flagged():
    flaws = _build(font_families=["Arial", "Georgia"])
    assert not any("different fonts" in f.description for f in flaws)


def test_stretched_images_flagged():
    flaws = _build(stretched_images=3)
    assert any("blurry" in f.description and "3 images are" in f.description for f in flaws)


def test_seo_analyzer_anchor_warnings_filtered_out():
    flaws = _build(seo_page={"warnings": ["Anchor missing title tag: #foo", "Description is too short"]})
    descriptions = [f.description for f in flaws]
    assert not any("Anchor missing title tag" in d for d in descriptions)
    assert any("Description is too short" in d for d in descriptions)


def test_no_booking_widget_flagged():
    flaws = _build(parsed={**_BASE_PARSED, "has_booking_widget": False})
    assert any(f.category == "conversion" and "booking" in f.description for f in flaws)


def test_no_click_to_call_or_whatsapp_flagged():
    flaws = _build(parsed={**_BASE_PARSED, "has_click_to_call": False, "has_whatsapp_link": False})
    assert any(f.category == "conversion" and "click-to-call" in f.description for f in flaws)


def test_click_to_call_alone_suppresses_conversion_flaw():
    # Either click-to-call OR WhatsApp is enough — shouldn't need both.
    flaws = _build(parsed={**_BASE_PARSED, "has_click_to_call": True, "has_whatsapp_link": False})
    assert not any("click-to-call" in f.description for f in flaws)


def test_no_menu_or_pricing_flagged():
    flaws = _build(parsed={**_BASE_PARSED, "has_menu_or_pricing": False})
    assert any(f.category == "conversion" and "pricing" in f.description for f in flaws)


def test_business_schema_type_missing_flagged_as_low():
    flaws = _build(has_structured_data=True, has_business_schema=False)
    matches = [f for f in flaws if "doesn't use a business type" in f.description]
    assert len(matches) == 1
    assert matches[0].severity == "low"


def test_business_schema_check_skipped_when_no_structured_data_at_all():
    # The generic "no structured data" flaw already covers this case —
    # shouldn't also fire the more specific business-schema-type flaw.
    flaws = _build(has_structured_data=False, has_business_schema=False)
    assert not any("doesn't use a business type" in f.description for f in flaws)
    assert any("No structured data" in f.description for f in flaws)
