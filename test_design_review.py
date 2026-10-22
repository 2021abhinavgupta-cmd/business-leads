"""The designer's view of a page, measured: typefaces, text sizes, heading order,
button shapes, line length, small text, first-screen call to action.
Pure rules first, then the probe in a real browser. Silence when it cannot measure."""
import pytest
from playwright.async_api import async_playwright

from analyzer import design_review
from analyzer.design_review import design_findings, design_positives

VIEWPORT = {"width": 1280, "height": 800}


def _keys(findings):
    return [f["key"] for f in findings]


def _probe(**over):
    base = {
        "fonts": {"Inter": 900, "Playfair Display": 100}, "sizes": {"16": 800, "32": 200},
        "chars": 1000, "smallChars": 0,
        "headings": [{"level": 1, "size": 40}, {"level": 2, "size": 28}],
        "buttons": ["rounded", "rounded"], "lineLengths": [70, 72, 68, 75],
        "interactive": 6, "firstScreenCta": True,
    }
    base.update(over)
    return base


# --- the rules ------------------------------------------------------------------

def test_a_well_built_page_has_no_findings_and_several_positives():
    p = _probe()
    assert design_findings(p) == []
    assert len(design_positives(p)) >= 4


@pytest.mark.parametrize("bad", [None, {}, "x", [], {"chars": 0}])
def test_no_measurement_means_silence(bad):
    assert design_findings(bad) == [] and design_positives(bad) == []


def test_many_typefaces_are_found_and_named():
    fonts = {n: 200 for n in ["Inter", "Satoshi", "Poppins", "Manrope", "Aeonik"]}
    out = design_findings(_probe(fonts=fonts, chars=1000))
    f = next(x for x in out if x["key"] == "many_typefaces")
    assert "5 different typefaces" in f["saw"] and "Satoshi" in f["saw"] and f["impact"] == "moderate"


def test_generic_icon_and_trace_fonts_are_not_counted():
    fonts = {"Inter": 900, "sans-serif": 500, "FontAwesome": 200, "Material Icons": 100,
             "Poppins": 20, "Manrope": 10, "Aeonik": 5, "Satoshi": 5}
    assert "many_typefaces" not in _keys(design_findings(_probe(fonts=fonts, chars=1740)))


def test_three_typefaces_is_fine():
    fonts = {"A": 400, "B": 400, "C": 200}
    assert "many_typefaces" not in _keys(design_findings(_probe(fonts=fonts)))


def test_a_dozen_text_sizes_are_found_with_their_range():
    sizes = {str(s): 100 for s in (11, 12, 13, 14, 15, 16, 18, 20, 24, 28, 32, 40)}
    out = design_findings(_probe(sizes=sizes, chars=1200))
    f = next(x for x in out if x["key"] == "many_text_sizes")
    assert "12 different text sizes" in f["saw"] and "11px to 40px" in f["saw"]


def test_small_text_share_is_reported_as_a_percentage():
    out = design_findings(_probe(smallChars=250))
    assert "25%" in next(x for x in out if x["key"] == "small_text")["title"]


def test_a_little_small_print_is_not_a_finding():
    assert "small_text" not in _keys(design_findings(_probe(smallChars=40)))


def test_a_missing_h1_is_found():
    out = design_findings(_probe(headings=[{"level": 2, "size": 28}]))
    assert "no_main_heading" in _keys(out)


def test_two_h1s_are_found():
    out = design_findings(_probe(headings=[{"level": 1, "size": 40}, {"level": 1, "size": 40}]))
    assert "many_main_headings" in _keys(out)


def test_a_skipped_heading_level_is_found():
    out = design_findings(_probe(headings=[{"level": 1, "size": 40}, {"level": 3, "size": 20}]))
    assert next(x for x in out if x["key"] == "skipped_heading_level")["title"] == "Headings jump from h1 to h3"


def test_three_button_shapes_are_found_two_are_not():
    assert "button_shapes" in _keys(design_findings(_probe(buttons=["square", "pill", "rounded", "pill"])))
    assert "button_shapes" not in _keys(design_findings(_probe(buttons=["square", "pill", "pill"])))


def test_wide_lines_need_a_median_over_the_limit_and_enough_paragraphs():
    assert "wide_text_lines" in _keys(design_findings(_probe(lineLengths=[110, 120, 100, 130])))
    assert "wide_text_lines" not in _keys(design_findings(_probe(lineLengths=[110, 120])))
    assert "wide_text_lines" not in _keys(design_findings(_probe(lineLengths=[110, 60, 70, 65])))


def test_no_first_screen_cta_needs_something_clickable_to_be_a_claim():
    assert "no_first_screen_cta" in _keys(design_findings(_probe(firstScreenCta=False, interactive=5)))
    assert "no_first_screen_cta" not in _keys(design_findings(_probe(firstScreenCta=False, interactive=0)))


def test_positives_are_only_said_when_measured():
    p = _probe(headings=[], firstScreenCta=False, lineLengths=[], smallChars=500)
    assert design_positives(p) == ["The text uses 2 typefaces (Inter and Playfair Display), "
                                   "which keeps the page looking like one brand."]


# --- the probe in a real browser ---------------------------------------------------

async def _measure(html: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport=VIEWPORT)
        try:
            await page.set_content(html)
            return await design_review.probe_design(page)
        finally:
            await browser.close()


_PARA = "Lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor incididunt. " * 4


async def test_the_probe_reads_real_fonts_sizes_headings_and_cta():
    probe = await _measure(
        "<body style='margin:0;font-family:Georgia'><h1 style='font-family:Arial;font-size:40px'>Acme studio</h1>"
        "<h3>Skipped</h3><a href='/contact'>Let's talk</a><p style='width:900px'>" + _PARA + "</p></body>")
    assert probe["chars"] > 100
    assert [h["level"] for h in probe["headings"]] == [1, 3]
    assert probe["firstScreenCta"] is True
    assert any(name in probe["fonts"] for name in ("Arial", "Georgia"))
    assert "skipped_heading_level" in _keys(design_findings(probe))


async def test_a_wide_paragraph_is_measured_in_characters_per_line():
    probe = await _measure(
        "<body style='margin:0'>" + "".join(
            f"<p style='width:1200px;font-size:14px'>{_PARA * 2}</p>" for _ in range(3)) + "</body>")
    assert probe["lineLengths"] and max(probe["lineLengths"]) > 95


async def test_button_corner_styles_are_bucketed():
    probe = await _measure(
        "<body><button style='border-radius:0;padding:10px 30px'>Square one</button>"
        "<button style='border-radius:8px;padding:10px 30px'>Rounded one</button>"
        "<button style='border-radius:999px;padding:10px 30px'>Pill one</button></body>")
    assert sorted(probe["buttons"]) == ["pill", "rounded", "square"]
    assert "button_shapes" in _keys(design_findings({**probe, "chars": 100}))


async def test_a_page_with_no_text_cannot_be_measured_and_says_nothing():
    probe = await _measure("<body></body>")
    assert design_findings(probe) == []

