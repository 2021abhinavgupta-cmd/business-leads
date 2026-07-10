"""
Integration smoke test for the full audit pipeline: Playwright screenshot ->
website audit -> decision-maker lookup -> AI analysis.

Hits a real website and (if keys are configured) real AI provider APIs, so
it's marked `integration` and skips itself when no AI provider key is set.
"""

import pytest

import config
from analyzer.ai_audit import AIAuditor
from analyzer.visuals import generate_audit_screenshot
from enrichment.decision_maker import DecisionMaker
from scrapers.website import WebsiteScraper

TEST_URL = (
    "https://www.timezonegames.com/en-in/locations/"
    "timezone-oberoi-mall-goregaon"
    "?utm_source=google&utm_medium=organic"
    "&utm_campaign=intz_20220420_googlemybusiness&utm_term"
)
TEST_COMPANY = "TIMEZONE"

_HAS_AI_KEY = bool(config.ANTHROPIC_API_KEY or config.GEMINI_API_KEY or config.OPENAI_API_KEY)


@pytest.mark.integration
@pytest.mark.skipif(not _HAS_AI_KEY, reason="No AI provider API key configured (ANTHROPIC/GEMINI/OPENAI_API_KEY)")
async def test_full_audit_pipeline():
    image_path, html_content, extra_audit_data = await generate_audit_screenshot(TEST_URL, TEST_COMPANY)
    assert html_content, "Expected Playwright to return rendered HTML"

    scraper = WebsiteScraper()
    web_data = await scraper.audit_website(TEST_URL, html=html_content, extra_audit_data=extra_audit_data)
    assert web_data.reachable is True

    dm = DecisionMaker()
    contact = dm.find_decision_maker(TEST_COMPANY, TEST_URL, html_content=html_content)
    assert isinstance(contact, dict)

    auditor = AIAuditor()
    analysis = auditor.analyze_lead(TEST_COMPANY, None, web_data, image_path=image_path)
    assert analysis is not None
    assert "overall_score" in analysis
    assert "flaws" in analysis
