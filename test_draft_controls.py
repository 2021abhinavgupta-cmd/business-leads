"""
Tests for per-draft send controls and the broken-link flaw wording
(added 2026-08-10).

Both came out of one live draft on yogabysuravi.com whose subject line read
"Your Yoga Classes Site Has 9 Broken Links and Mobile Issues" and whose body
said the Marine Lines page "just returns a dead end". That URL answers 200 to
every method and user agent tried, and re-probing the whole site found the
only failing hosts to be facebook.com (400) and linkedin.com (999) — both
anti-bot blocks, both already excluded by _BOT_HOSTILE_HOSTS.

Two things about the flaw text made that draft worse than it had to be:

  - the count was rendered "N broken link(s)/image(s)", which the AI reliably
    compressed to "N broken links" even when images were in the total
  - naming an example URL invited the AI to describe that specific page from
    imagination; we recorded a failed request, never the page's contents

And the human had no way to correct it: the body was editable, but the
subject line was display-only and the screenshot was attached unconditionally.

Unit tests only — no network.
"""

import inspect
import re

import pytest

from scrapers.website import WebsiteScraper


_BASE = dict(
    load_time_ms=1200, perf_score=57, seo_score=77, mobile_score=60,
    best_practices_score=80, has_ssl=True,
    parsed={"has_cta": True, "has_contact": True, "has_testimonials": True,
            "has_blog": True, "meta_title": "Acme", "meta_description": "d",
            "h1_tags": ["h"], "homepage_text": "x"},
    has_structured_data=True, readability_score=60, security_flaws=[],
    seo_page={}, accessibility_violations=[],
)


def _broken_flaw(broken_links):
    flaws = WebsiteScraper._build_flaws(broken_links=broken_links, **_BASE)
    matches = [f for f in flaws if "broken" in f.description]
    return matches[0].description if matches else ""


# ---------------------------------------------------------------------------
# Links and images are counted separately
# ---------------------------------------------------------------------------

def test_links_and_images_are_reported_as_separate_counts():
    """
    "9 broken link(s)/image(s)" became "9 Broken Links" in a real subject
    line. If images are in the total, the copy has to be able to say so.
    """
    text = _broken_flaw(
        [{"type": "link", "url": "https://x.in/a"}] * 7
        + [{"type": "image", "url": "https://x.in/i.png"}] * 2
    )
    assert "7 broken links" in text
    assert "2 broken images" in text


def test_links_only_never_mentions_images():
    text = _broken_flaw([{"type": "link", "url": "https://x.in/a"}] * 3)
    assert "3 broken links" in text
    assert "image" not in text.split("do NOT")[0]


def test_images_only_never_mentions_links():
    text = _broken_flaw([{"type": "image", "url": "https://x.in/i.png"}] * 2)
    assert "2 broken images" in text
    assert "broken link" not in text


@pytest.mark.parametrize("count, expected", [(1, "1 broken link "), (2, "2 broken links")])
def test_singular_and_plural_are_both_correct(count, expected):
    text = _broken_flaw([{"type": "link", "url": "https://x.in/a"}] * count)
    assert expected in text


def test_the_old_slashed_wording_is_gone():
    """
    It is what the AI collapsed into a wrong, confident number. Asserted
    against the generated text rather than the source, since the source
    quotes the old wording in the comment explaining why it changed.
    """
    for links in ([{"type": "link", "url": "https://x.in/a"}],
                  [{"type": "image", "url": "https://x.in/i.png"}],
                  [{"type": "link", "url": "https://x.in/a"},
                   {"type": "image", "url": "https://x.in/i.png"}]):
        assert "link(s)" not in _broken_flaw(links)
        assert "image(s)" not in _broken_flaw(links)


# ---------------------------------------------------------------------------
# The example URL must not invite invention
# ---------------------------------------------------------------------------

def test_the_example_url_is_still_given():
    """A concrete example is what makes the flaw actionable."""
    assert "https://x.in/a" in _broken_flaw([{"type": "link", "url": "https://x.in/a"}])


def test_the_flaw_forbids_describing_the_example_page():
    """
    We recorded a failed request, not the contents of the page. The live
    draft said the Marine Lines page "just returns a dead end" about a URL
    that answers 200 to every method and user agent tried.
    """
    text = _broken_flaw([{"type": "link", "url": "https://x.in/a"}])
    assert "do NOT" in text
    assert "describe what any specific page does" in text
    assert "claim to have visited it" in text


def test_severity_still_scales_with_the_total():
    many = [{"type": "link", "url": f"https://x.in/{n}"} for n in range(9)]
    few = [{"type": "link", "url": "https://x.in/a"}]
    assert WebsiteScraper._build_flaws(broken_links=many, **_BASE)
    high = next(f for f in WebsiteScraper._build_flaws(broken_links=many, **_BASE) if "broken" in f.description)
    medium = next(f for f in WebsiteScraper._build_flaws(broken_links=few, **_BASE) if "broken" in f.description)
    assert high.severity == "high"
    assert medium.severity == "medium"


# ---------------------------------------------------------------------------
# Per-draft send controls
# ---------------------------------------------------------------------------

def _app_source():
    return open("app.py", encoding="utf-8").read()


def _jsx():
    return open("frontend/src/App.jsx", encoding="utf-8").read()


def test_send_accepts_an_attach_screenshot_flag():
    assert "attach_screenshot: bool = True" in _app_source(), (
        "must default True so existing callers are unaffected"
    )


def test_the_flag_actually_suppresses_the_attachment():
    source = _app_source()
    assert "if not req.attach_screenshot:" in source
    # The suppression has to come before the path lookup, not after.
    assert source.index("if not req.attach_screenshot:") < source.index("make_screenshot_filename(req.company")


def test_both_send_paths_forward_the_flag():
    """The audit-results view and the saved-drafts view are separate calls."""
    assert _jsx().count("attach_screenshot: ") >= 2


def test_removing_the_image_is_reversible():
    """A destructive-looking control that cannot be undone is a trap."""
    jsx = _jsx()
    assert "Put it back" in jsx
    assert "attach_screenshot = true" in jsx or "attach_screenshot: true" in jsx


def test_the_subject_is_editable_in_both_views():
    """
    It was display-only while the body was editable, so a wrong subject meant
    discarding the whole draft — and the subject is the line the recipient
    actually sees first.
    """
    jsx = _jsx()
    assert jsx.count('className="subject-editor"') == 2
    assert "<input" in jsx


def test_the_subject_is_no_longer_rendered_as_static_text_in_the_draft_views():
    jsx = _jsx()
    assert '<strong>Subject:</strong> {draft.subject}' not in jsx
    assert '<strong>Subject:</strong> {lead.auditData.subject}' not in jsx


def test_the_edited_subject_is_what_gets_sent():
    """An editor whose value never reaches the payload is worse than none."""
    jsx = _jsx()
    assert "subject: lead.auditData.subject," in jsx
    assert "subject: draft.subject," in jsx


def test_the_new_controls_are_styled():
    css = open("frontend/src/App.css", encoding="utf-8").read()
    for selector in (".subject-editor", ".remove-image-btn", ".image-removed-note"):
        assert selector in css, f"{selector} has no styling"
