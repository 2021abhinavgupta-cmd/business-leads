"""
The "what a visitor sees" checks: a first screen that is only a video player,
and project cards that send visitors to Drive / Instagram. Found by a
third-party audit of mmga.agency; measured in the DOM, never judged by a model.

Pure rule tests first, then real-browser tests of the probe on local HTML
(no network, no API key; only Chromium, which CI already installs).
"""
import pytest
from playwright.async_api import async_playwright

from analyzer import visuals
from analyzer.visuals import _presentation_findings
from scrapers.website import WebsiteScraper

VIEWPORT = {"width": 1280, "height": 800}


def _keys(findings):
    return [f["key"] for f in findings]


# --- the rules (no browser) -------------------------------------------------

def test_a_video_first_screen_with_no_headline_is_a_serious_finding():
    out = _presentation_findings({"heroMedia": True, "heroMediaCover": 0.8, "hasHeadline": False})
    assert _keys(out) == ["hero_video_no_headline"]
    assert out[0]["impact"] == "serious" and out[0]["selector"] is None


def test_a_small_clip_is_not_a_first_screen_claim():
    out = _presentation_findings({"heroMedia": True, "heroMediaCover": 0.1, "hasHeadline": False})
    assert out == []


def test_a_headline_means_no_hero_finding():
    out = _presentation_findings({"heroMedia": True, "heroMediaCover": 0.9, "hasHeadline": True})
    assert out == []


def test_no_media_and_no_headline_is_a_moderate_finding():
    out = _presentation_findings({"heroMedia": False, "hasHeadline": False})
    assert _keys(out) == ["no_headline_first_screen"] and out[0]["impact"] == "moderate"


def test_three_cards_that_leave_the_site_are_a_finding_with_a_selector():
    out = _presentation_findings({
        "hasHeadline": True, "proofCards": 5, "proofFileLinks": 0,
        "proofHosts": ["drive.google.com", "instagram.com"], "proofSelector": '[data-mmga-flaw="x"]',
    })
    assert _keys(out) == ["proof_leaves_site"]
    assert "open a Google Drive file or an Instagram profile instead of a page" in out[0]["description"]
    assert out[0]["selector"] and out[0]["scroll"] is True


def test_two_cards_alone_are_not_enough():
    assert _presentation_findings({"hasHeadline": True, "proofCards": 2, "proofFileLinks": 0}) == []


@pytest.mark.parametrize("bad", [None, {}, "x", []])
def test_a_missing_or_malformed_probe_reports_nothing_about_cards(bad):
    # No probe is "could not measure", never a finding. ({} has no headline key,
    # which the rules read as "none found", so only the cards rule is asserted.)
    out = _presentation_findings(bad)
    assert "proof_leaves_site" not in _keys(out)
    if bad is None or not isinstance(bad, dict):
        assert out == []


# --- the flaws reach the ranked list ----------------------------------------

def test_findings_become_conversion_flaws_with_mapped_severity():
    from test_build_flaws import _build
    flaws = _build(presentation_findings=[
        {"key": "a", "description": "The first screen is a video player", "impact": "serious"},
        {"key": "b", "description": "Some other thing", "impact": "moderate"},
    ])
    found = {f.description: f for f in flaws if f.category == "conversion"}
    assert found["The first screen is a video player."].severity == "high"
    assert found["Some other thing."].severity == "medium"


def test_a_clean_site_still_has_no_flaws_with_no_findings():
    from test_build_flaws import _build
    assert _build(presentation_findings=[]) == []


# --- the probe in a real browser --------------------------------------------

async def _probe(html: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport=VIEWPORT)
        try:
            await page.set_content(html)
            findings = await visuals._probe_presentation(page)
            shots = await visuals._capture_presentation_shots(page, findings)
        finally:
            await browser.close()
        return findings, shots


_PLAYER = '<iframe src="about:blank" style="display:block;width:100%;height:760px;border:0"></iframe>'


async def test_a_real_video_only_first_screen_is_found():
    findings, _ = await _probe(f"<body style='margin:0'><nav><a href='#'>Work</a></nav>{_PLAYER}</body>")
    assert "hero_video_no_headline" in _keys(findings)


async def test_a_real_h1_above_the_player_clears_it():
    findings, _ = await _probe(
        f"<body style='margin:0'><h1>We build brands that grow</h1>{_PLAYER.replace('760', '500')}</body>"
    )
    assert findings == []


async def test_big_text_in_a_div_counts_as_a_headline():
    findings, _ = await _probe(
        "<body style='margin:0'><div style='font-size:40px'>Branding for growing companies</div>"
        + _PLAYER.replace("760", "500") + "</body>"
    )
    assert "hero_video_no_headline" not in _keys(findings)


_PIXEL = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"


def _cards(hrefs):
    # A project tile: an image with one link, card sized.
    return "".join(
        f"<div style='width:200px;height:160px;float:left'><img src='{_PIXEL}' "
        f"style='width:200px;height:120px'><a href='{h}' target='_blank'>open</a></div>" for h in hrefs
    )


async def test_cards_that_open_drive_and_instagram_are_found_and_boxed():
    findings, shots = await _probe(
        "<body style='margin:0'><h1>Our work for brands</h1><div style='height:1200px'>space</div>"
        + _cards(["https://drive.google.com/file/d/1", "https://instagram.com/zomato/",
                  "https://drive.google.com/file/d/2", "https://linkedin.com/company/x"])
        + "<div style='clear:both;height:1200px'>more page below the cards</div></body>"
    )
    assert "proof_leaves_site" in _keys(findings)
    assert len(shots) == 1 and shots[0]["screenshot_bytes"]
    assert shots[0]["visual_flaw"]["description"].startswith("4 of the project cards")


async def test_small_footer_social_icons_are_not_cards():
    icons = "".join(
        f"<a href='{h}' style='display:inline-block;width:24px;height:24px'>i</a>"
        for h in ["https://instagram.com/acme/", "https://linkedin.com/company/acme",
                  "https://behance.net/acme", "https://dribbble.com/acme"]
    )
    findings, _ = await _probe(f"<body style='margin:0'><h1>Acme dental care</h1><footer>{icons}</footer></body>")
    assert "proof_leaves_site" not in _keys(findings)


async def test_a_social_row_outside_a_footer_tag_is_not_cards():
    # One big container with an image and four social links: not four tiles.
    row = "".join(f"<a href='{h}'>s</a>" for h in [
        "https://instagram.com/acme/", "https://linkedin.com/company/acme",
        "https://behance.net/acme", "https://dribbble.com/acme"])
    findings, _ = await _probe(
        f"<body style='margin:0'><h1>Acme studio</h1><div style='width:600px;height:300px'>"
        f"<img src='{_PIXEL}' style='width:300px;height:200px'>{row}</div></body>"
    )
    assert "proof_leaves_site" not in _keys(findings)


async def test_hover_revealed_links_still_count():
    # Tiles whose link is invisible until hover (opacity 0) are still tiles.
    tiles = "".join(
        f"<div style='width:200px;height:160px;float:left'><img src='{_PIXEL}' style='width:200px;height:120px'>"
        f"<a href='{h}' style='opacity:0'>open</a></div>"
        for h in ["https://drive.google.com/file/d/1", "https://instagram.com/a/", "https://instagram.com/b/"]
    )
    findings, _ = await _probe(f"<body style='margin:0'><h1>Our work</h1>{tiles}<div style='clear:both;height:1200px'>x</div></body>")
    assert "proof_leaves_site" in _keys(findings)


async def test_an_instagram_post_feed_is_not_a_portfolio():
    findings, _ = await _probe(
        "<body style='margin:0'><h1>Acme cafe</h1>"
        + _cards([f"https://instagram.com/p/post{i}/" for i in range(6)]) + "</body>"
    )
    assert "proof_leaves_site" not in _keys(findings)
