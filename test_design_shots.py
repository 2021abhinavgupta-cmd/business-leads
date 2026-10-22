"""Pictures for the designer-view findings: crops come from the real page, labelled, and absent when unmakeable."""
import io

from PIL import Image

from analyzer import design_review
from reporting import design_shots, report as rep


def _png(w=300, h=60, color=(240, 240, 240)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (w, h), color).save(b, "PNG")
    return b.getvalue()


def test_a_collage_stacks_labelled_crops_into_one_jpeg():
    out = design_shots.build_collage([("Inter", _png()), ("Playfair Display", _png(1400, 400))])
    img = Image.open(io.BytesIO(out))
    assert img.format == "JPEG" and img.width == 960
    assert img.height > 2 * (30 + 60)               # both tiles present, tall crops capped at 110px


def test_a_collage_with_nothing_usable_is_none():
    assert design_shots.build_collage([]) is None
    assert design_shots.build_collage([("x", b"not an image")]) is None


def test_a_design_finding_gets_its_picture_and_caption_in_the_report():
    probe = {"fonts": {n: 200 for n in "ABCDE"}, "headings": [], "chars": 1000, "sizes": {}, "lineLengths": []}
    shots = {"many_typefaces": {"image": b"JPEG", "caption": "A line in each typeface."}}
    r = rep.build_report("A", "a.com", {"design_probe": probe, "design_shots": shots, "desktop_image": b"DESK"})
    by_key = {f["key"]: f for f in r["findings"]}
    assert by_key["many_typefaces"]["image"] == b"JPEG" and by_key["many_typefaces"]["caption"] == "A line in each typeface."
    assert by_key["no_main_heading"]["image"] == b"DESK"          # an absence is shown where it is missing


def test_without_pictures_the_findings_are_text_only():
    probe = {"fonts": {n: 200 for n in "ABCDE"}, "headings": [{"level": 1, "size": 40}], "chars": 1000,
             "sizes": {}, "lineLengths": []}
    r = rep.build_report("A", "a.com", {"design_probe": probe})
    assert all(f["image"] is None for f in r["findings"])


_PAGE = """<html><body style="margin:0;font:16px/1.4 Arial,sans-serif">
<h1>First main heading</h1><h1>Second main heading</h1><h3>A level that was skipped</h3>
<div style="width:1300px"><p>{long}</p><p>{long}</p><p>{long}</p></div>
<p style="font-family:Georgia,serif;font-size:22px">A serif sample line of text</p>
<p style="font-family:Courier New,monospace;font-size:20px">A monospace sample line</p>
<button style="border-radius:0;padding:10px 24px">Square</button>
<button style="border-radius:8px;padding:10px 24px">Rounded</button>
<button style="border-radius:40px;padding:10px 24px">Pill</button>
</body></html>""".replace("{long}", "word " * 90)


async def _shots(findings_keys):
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        await page.set_content(_PAGE)
        probe = await design_review.probe_design(page)
        shots = await design_shots.capture(page, probe, [{"key": k} for k in findings_keys])
        leftover = await page.evaluate(
            "() => document.querySelectorAll('[data-mmga-d], .mmga-d-ov').length")
        await browser.close()
    return shots, leftover


async def test_real_page_pictures_are_made_and_the_page_is_left_clean():
    keys = ["many_main_headings", "skipped_heading_level", "wide_text_lines", "button_shapes", "many_typefaces"]
    shots, leftover = await _shots(keys)
    assert set(shots) == set(keys)
    for s in shots.values():
        assert Image.open(io.BytesIO(s["image"])).format == "JPEG" and s["caption"]
    assert leftover == 0                      # no marker or overlay survives into later measurements


async def test_a_finding_with_nothing_to_show_gets_no_picture():
    shots, _ = await _shots(["small_text"])   # the page has no small text
    assert shots == {}

