"""
Tests for checks that infer a problem from something being ABSENT
(added 2026-08-09).

Most silent-degradation bugs in this project run one direction: a tool
fails, fewer flaws are found, and the site looks HEALTHIER than it is. The
security-header check ran the other way. Every one of its rules concludes
"this header is missing" from a key not being in a dict, so when the dict
was empty because nothing was captured, it fired all five at once and
invented a security problem on a site that may well be configured correctly.

Measured before the fix: 5 flaws (2 high, 2 medium, 1 low) from an empty
dict, versus 1 for a genuinely well-configured site. That is enough score
deflation to push a healthy lead below CONTACT_THRESHOLD and then email
them five criticisms nothing ever measured.

Also covers the two other checks whose failure was indistinguishable from a
clean result and which nothing recorded: the broken-link scan and robots.txt.

Unit tests only — no network, no browser.
"""

import inspect

import pytest

from scrapers.website import WebsiteScraper


_WELL_CONFIGURED = {
    "date": "Sat, 09 Aug 2026 12:00:00 GMT",
    "content-type": "text/html; charset=utf-8",
    "strict-transport-security": "max-age=63072000",
    "x-frame-options": "DENY",
    "content-security-policy": "default-src 'self'",
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
}


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------

def test_uncaptured_headers_produce_no_flaws_at_all(capsys):
    """
    The bug: {} meant "we didn't capture any headers", but every rule read it
    as "this header is missing" and fired.
    """
    flaws = WebsiteScraper._check_security_headers({}, has_ssl=True)
    assert flaws == []
    assert "skipping security-header checks" in capsys.readouterr().out


def test_none_headers_produce_no_flaws():
    assert WebsiteScraper._check_security_headers(None, has_ssl=True) == []


def test_a_well_configured_site_is_mostly_clean():
    flaws = WebsiteScraper._check_security_headers(_WELL_CONFIGURED, has_ssl=True)
    assert flaws == []


def test_genuinely_missing_headers_are_still_reported():
    """The fix must not blunt the check when headers WERE captured."""
    headers = {"date": "Sat, 09 Aug 2026 12:00:00 GMT", "content-type": "text/html"}
    flaws = WebsiteScraper._check_security_headers(headers, has_ssl=True)

    assert len(flaws) >= 4, "a real response missing every security header is a real finding"
    assert any("HSTS" in f.description for f in flaws)


def test_a_real_response_is_never_empty_which_is_what_makes_the_check_safe():
    """
    The discriminator only works because a real HTTP response always carries
    SOME header (Date, Content-Type, Server). A site that sets no *security*
    headers still yields a populated dict, so emptiness can only mean "not
    captured".
    """
    minimal_real_response = {"date": "Sat, 09 Aug 2026 12:00:00 GMT"}
    assert WebsiteScraper._check_security_headers(minimal_real_response, has_ssl=True) != []


def test_hsts_is_not_demanded_of_a_plain_http_site():
    headers = {"date": "x", "content-type": "text/html"}
    flaws = WebsiteScraper._check_security_headers(headers, has_ssl=False)
    assert not any("HSTS" in f.description for f in flaws)


# ---------------------------------------------------------------------------
# robots.txt — a failed fetch is not "not blocked"
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


class _FakeClient:
    def __init__(self, response=None, raises=False):
        self._response = response
        self._raises = raises

    async def get(self, *a, **k):
        if self._raises:
            raise RuntimeError("connection reset")
        return self._response


def _scraper(client) -> WebsiteScraper:
    scraper = WebsiteScraper.__new__(WebsiteScraper)
    scraper.client = client
    return scraper


async def test_a_blanket_disallow_is_detected_and_marked_checked():
    scraper = _scraper(_FakeClient(_FakeResponse(200, "User-agent: *\nDisallow: /")))
    blocked, checked = await scraper._check_robots_disallow_all("https://example.com")
    assert blocked is True
    assert checked is True


async def test_a_normal_robots_file_is_not_blocked_and_is_marked_checked():
    scraper = _scraper(_FakeClient(_FakeResponse(200, "User-agent: *\nDisallow: /admin")))
    blocked, checked = await scraper._check_robots_disallow_all("https://example.com")
    assert blocked is False
    assert checked is True


async def test_a_missing_robots_file_counts_as_a_real_answer():
    """404 is definite: nothing is disallowed. Unlike a network failure."""
    scraper = _scraper(_FakeClient(_FakeResponse(404)))
    blocked, checked = await scraper._check_robots_disallow_all("https://example.com")
    assert blocked is False
    assert checked is True


async def test_a_fetch_failure_is_not_reported_as_not_blocked():
    scraper = _scraper(_FakeClient(raises=True))
    blocked, checked = await scraper._check_robots_disallow_all("https://example.com")
    assert blocked is False
    assert checked is False, "a timeout must not look identical to a clean result"


async def test_a_server_error_is_not_reported_as_checked():
    scraper = _scraper(_FakeClient(_FakeResponse(503)))
    _, checked = await scraper._check_robots_disallow_all("https://example.com")
    assert checked is False


async def test_a_disallow_for_another_bot_does_not_count():
    robots = "User-agent: BadBot\nDisallow: /\n\nUser-agent: *\nDisallow: /tmp"
    scraper = _scraper(_FakeClient(_FakeResponse(200, robots)))
    blocked, checked = await scraper._check_robots_disallow_all("https://example.com")
    assert blocked is False
    assert checked is True


# ---------------------------------------------------------------------------
# Coverage wiring — a failure must be visible, not just harmless
# ---------------------------------------------------------------------------

def test_the_three_previously_untracked_signals_are_recorded():
    """
    Each degrades to a value indistinguishable from a clean result, so
    without a signal_status entry the AI is free to imply those areas are
    fine when they were never measured.
    """
    source = inspect.getsource(WebsiteScraper.audit_website)
    for signal in ("security_headers", "broken_links", "robots_txt"):
        assert f'signal_status["{signal}"]' in source, f"{signal} coverage is untracked"


def test_broken_link_scan_reports_how_much_it_actually_checked():
    from analyzer import visuals

    source = inspect.getsource(visuals._check_broken_assets)
    assert "checked" in source
    assert "return broken[:10], checked" in source, (
        "an empty list must be distinguishable from a scan that never ran"
    )
