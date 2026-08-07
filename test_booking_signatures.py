"""
Unit tests for the booking-widget/menu-pricing signature list expanded
2026-08-07 — no network, no API keys. This project's leads are
overwhelmingly Indian SMBs, and the original 14-signature list skewed
US-restaurant-heavy (OpenTable, Resy, ...), missing the platforms that
actually dominate Indian clinic/dentist/salon booking (Practo, Fresha,
Vagaro, ...). Locks in that the new additions are wired correctly rather
than just present in the list but never actually checked against.
"""

import pytest

from scrapers.website import WebsiteScraper


def _html_with_script(src: str) -> str:
    return f'<html><head><script src="{src}"></script></head><body>Hi</body></html>'


@pytest.mark.parametrize("src", [
    "https://www.practo.com/widget.js",
    "https://www.fresha.com/embed.js",
    "https://booking.vagaro.com/widget.js",
    "https://widgets.mindbodyonline.com/loader.js",
])
async def test_new_booking_platforms_are_detected(src):
    scraper = WebsiteScraper()
    parsed = await scraper._parse_html(_html_with_script(src))
    assert parsed["has_booking_widget"] is True


async def test_unknown_booking_platform_is_not_detected():
    """Documents the known limitation (see CLAUDE.md §8): a platform not on
    the list still reads as "no booking widget", which is the whole reason
    this check is heuristic, not exhaustive."""
    scraper = WebsiteScraper()
    parsed = await scraper._parse_html(_html_with_script("https://some-niche-booking-tool.example/widget.js"))
    assert parsed["has_booking_widget"] is False


@pytest.mark.parametrize("html,expected", [
    ('<a href="/packages">Packages</a>', True),
    ('<a href="/rate-card">Rate Card</a>', True),
    ('<a href="/menu">Menu</a>', True),
    ('<a href="/about">About</a>', False),
])
async def test_menu_pricing_keywords(html, expected):
    scraper = WebsiteScraper()
    parsed = await scraper._parse_html(f"<html><body>{html}</body></html>")
    assert parsed["has_menu_or_pricing"] is expected
