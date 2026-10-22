"""Footer "built by" detection (idea from Harvey, MIT). Silence beats a wrong name."""
from analyzer.agency_credit import detect_agency


def _page(footer: str) -> str:
    return f"<html><body><h1>Acme</h1><footer>{footer}</footer></body></html>"


def test_website_by_credit_is_found():
    out = detect_agency(_page('© 2026 Acme. Website by <a href="https://pixelcraft.in">PixelCraft</a>'), "acme.com")
    assert out["name"] == "PixelCraft" and out["confidence"] >= 0.9


def test_a_descriptive_anchor_is_found_with_lower_confidence():
    out = detect_agency(_page('<a href="https://zoomweb.in">Web Design Mumbai</a>'), "acme.com")
    assert out and out["confidence"] == 0.75


def test_a_bare_external_link_is_not_a_credit():
    assert detect_agency(_page('<a href="https://partner.com">Partner</a>'), "acme.com") is None


def test_social_and_widget_links_are_never_the_agency():
    html = _page('Site by <a href="https://instagram.com/acme">Acme on IG</a>'
                 '<a href="https://wordpress.org">WordPress</a>')
    assert detect_agency(html, "acme.com") is None


def test_the_sites_own_domain_is_not_an_agency():
    assert detect_agency(_page('Built by <a href="https://www.acme.com/team">Acme team</a>'), "acme.com") is None


def test_script_text_is_ignored():
    html = _page("<script>var s='Website by <a href=\"https://evil.io\">Evil</a>';</script>")
    assert detect_agency(html, "acme.com") is None


def test_empty_input_is_none():
    assert detect_agency("", "acme.com") is None and detect_agency("<p>x</p>", "") is None
