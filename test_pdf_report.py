"""The PDF report: what it says is built from measurements only, and the PDF renders.
build_report / render_html are pure; one test renders a real PDF in Chromium."""
import io
from datetime import date

import pytest
from PIL import Image

from reporting import report as rep

PROBE = {
    "fonts": {"Inter": 900, "Playfair Display": 100}, "sizes": {"16": 800, "32": 200},
    "chars": 1000, "smallChars": 0,
    "headings": [{"level": 1, "size": 40}], "buttons": ["rounded"], "lineLengths": [70, 72, 68],
    "interactive": 5, "firstScreenCta": True,
}


def _jpeg(color=(200, 120, 60)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (400, 250), color).save(b, "JPEG")
    return b.getvalue()


def _collected(**over):
    base = {
        "final_url": "https://acme.com/", "presentation_findings": [], "design_probe": PROBE,
        "real_web_vitals": {"lcp_ms": 1200}, "mobile_overflow": False, "mobile_scroll_width": 390,
        "desktop_image": _jpeg(), "mobile_image": _jpeg((60, 120, 200)), "card_image": None,
        "seo_findings": [],
    }
    base.update(over)
    return base


def _keys(report):
    return [f["key"] for f in report["findings"]]


def test_a_clean_site_has_no_findings_and_says_what_works():
    r = rep.build_report("Acme", "acme.com", _collected())
    assert r["findings"] == []
    text = " ".join(r["positives"])
    assert "2 typefaces" in text and "1.2 seconds" in text and "https" in text and "phone screen" in text


def test_visitor_view_findings_become_numbered_findings_with_their_pictures():
    pres = [
        {"key": "hero_video_no_headline", "description": "The first screen is a video player", "impact": "serious"},
        {"key": "contact_form_no_reply_time", "description": "The form says nothing about replies", "impact": "minor"},
    ]
    r = rep.build_report("Acme", "acme.com", _collected(presentation_findings=pres))
    assert _keys(r) == ["hero_video_no_headline", "contact_form_no_reply_time"]
    assert [f["number"] for f in r["findings"]] == [1, 2]
    assert r["findings"][0]["tag"] == "Fix first" and r["findings"][0]["image"] == r["desktop_image"]
    assert r["findings"][1]["image"] is None


def test_the_card_finding_uses_the_card_screenshot_and_is_text_only_without_one():
    pres = [{"key": "proof_leaves_site", "description": "4 of the project cards open Drive", "impact": "serious"}]
    with_img = rep.build_report("A", "a.com", _collected(presentation_findings=pres, card_image=_jpeg()))
    without = rep.build_report("A", "a.com", _collected(presentation_findings=pres))
    assert with_img["findings"][0]["image"] and without["findings"][0]["image"] is None


def test_findings_are_ordered_most_important_first():
    pres = [{"key": "contact_form_no_reply_time", "description": "x", "impact": "minor"},
            {"key": "hero_video_no_headline", "description": "y", "impact": "serious"}]
    assert _keys(rep.build_report("A", "a.com", _collected(presentation_findings=pres)))[0] == "hero_video_no_headline"


def test_a_sideways_scrolling_phone_page_is_a_finding_only_when_measured():
    r = rep.build_report("A", "a.com", _collected(mobile_overflow=True, mobile_scroll_width=520))
    f = next(x for x in r["findings"] if x["key"] == "mobile_sideways")
    assert "520px" in f["saw"] and f["image"] == r["mobile_image"]
    unknown = rep.build_report("A", "a.com", _collected(mobile_overflow=None))
    assert "mobile_sideways" not in _keys(unknown)
    assert not any("phone screen" in p for p in unknown["positives"])      # unmeasured is not a strength


def test_designer_findings_are_included_with_their_numbers():
    probe = {**PROBE, "fonts": {n: 200 for n in "ABCDE"}, "headings": []}
    r = rep.build_report("A", "a.com", _collected(design_probe=probe))
    assert {"many_typefaces", "no_main_heading"} <= set(_keys(r))
    assert "5 different typefaces" in next(f for f in r["findings"] if f["key"] == "many_typefaces")["saw"]


def test_a_slow_reading_goes_in_the_appendix_with_its_device_not_in_the_findings():
    r = rep.build_report("A", "a.com", _collected(real_web_vitals={"lcp_ms": 6200}))
    assert "speed" not in " ".join(_keys(r)) and any("6.2 seconds in our test browser" in a for a in r["appendix"])
    assert not any("seconds" in p for p in r["positives"])


def test_search_probe_findings_go_in_the_appendix():
    seo = [{"key": "x_robots_noindex", "description": "The server tells search engines not to list this page"}]
    r = rep.build_report("A", "a.com", _collected(seo_findings=seo))
    assert r["appendix"] == ["The server tells search engines not to list this page."]


def test_nothing_measured_gives_an_empty_but_valid_report():
    r = rep.build_report("A", "a.com", {})
    assert r["findings"] == [] and r["positives"] == [] and r["appendix"] == []
    assert "<html" in rep.render_html(r)


def test_html_escapes_company_and_text():
    r = rep.build_report('Acme <b>&"', "a.com", _collected())
    html = rep.render_html(r)
    assert "<b>&" not in html and "Acme &lt;b&gt;&amp;&quot;" in html


def test_html_has_cover_summary_finding_and_closing_pages_and_inlines_images():
    pres = [{"key": "hero_video_no_headline", "description": "The first screen is a video player", "impact": "serious"}]
    html = rep.render_html(rep.build_report("Acme", "acme.com", _collected(presentation_findings=pres),
                                            contact_email="hi@mmga.agency", today=date(2026, 10, 22)))
    assert html.count('class="page') == 4          # cover, summary, one finding with a picture, closing
    assert "22 October 2026" in html and "hi@mmga.agency" in html and "data:image/jpeg;base64," in html
    assert "http://" not in html.replace("https://", "")        # no remote resources


async def test_a_real_pdf_is_produced_with_the_findings_in_it():
    import pdfplumber
    pres = [{"key": "hero_video_no_headline", "description": "The first screen is a video player", "impact": "serious"}]
    html = rep.render_html(rep.build_report("Acme", "acme.com", _collected(presentation_findings=pres)))
    pdf = await rep.render_pdf(html)
    assert pdf[:4] == b"%PDF"
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        assert len(doc.pages) == 4
        text = " ".join((p.extract_text() or "") for p in doc.pages)
    assert "THE FIRST SCREEN IS A VIDEO PLAYER" in text.upper() and "ACME" in text.upper()
