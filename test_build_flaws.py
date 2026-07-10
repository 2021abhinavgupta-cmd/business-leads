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
        {"impact": "critical", "help": "Critical issue", "nodes_count": 1},
        {"impact": "serious", "help": "Serious issue", "nodes_count": 2},
        {"impact": "moderate", "help": "Moderate issue", "nodes_count": 3},
        {"impact": "minor", "help": "Minor issue", "nodes_count": 4},
    ]
    flaws = _build(accessibility_violations=violations)
    by_desc = {f.description: f.severity for f in flaws}
    assert by_desc["[CRITICAL] Critical issue (1 instance(s) on the page)"] == "critical"
    assert by_desc["[SERIOUS] Serious issue (2 instance(s) on the page)"] == "high"
    assert by_desc["[MODERATE] Moderate issue (3 instance(s) on the page)"] == "medium"
    assert by_desc["[MINOR] Minor issue (4 instance(s) on the page)"] == "low"


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
