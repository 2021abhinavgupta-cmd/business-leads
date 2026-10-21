import io
import re

import pytest
from PIL import Image, ImageDraw

from importer import email_html, email_images

MAGENTA = (255, 0, 255)  # analyzer/visuals.py _HIGHLIGHT_COLOR
BOX_PX = 4  # analyzer/visuals.py border:4px


DEFAULT_BOX = (300, 200, 800, 500)


def _make_page(path, size=(1280, 800), box=DEFAULT_BOX, width=BOX_PX):
    img = Image.new("RGB", size, (255, 248, 239))
    d = ImageDraw.Draw(img)
    for y in range(40, size[1], 60):
        d.line([(40, y), (size[0] - 40, y)], fill=(160, 160, 160), width=2)
    if box:
        d.rectangle(box, outline=MAGENTA, width=width)
    img.save(path, "PNG")
    return path


def _near(px, tol=90):
    return all(abs(a - b) <= tol for a, b in zip(px, MAGENTA))


def _decode(data):
    return Image.open(io.BytesIO(data)).convert("RGB")


def test_prepare_returns_jpeg_at_most_600_wide_and_under_90kb(tmp_path):
    p = _make_page(str(tmp_path / "a.png"))
    data = email_images.prepare(p)
    img = Image.open(io.BytesIO(data))
    assert img.format == "JPEG"
    assert img.width <= 600
    assert len(data) <= 90_000


def test_prepare_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        email_images.prepare(str(tmp_path / "nope.png"))
    with pytest.raises(FileNotFoundError):
        email_images.prepare_all([{"path": str(tmp_path / "nope.png")}])


def _side_hits(img, box, scale):
    x0, y0, x1, y1 = [v * scale for v in box]
    hw = BOX_PX * scale / 2
    n = 12
    sides = {
        "top": [(x0 + (x1 - x0) * i / n, y0 + hw) for i in range(1, n)],
        "bottom": [(x0 + (x1 - x0) * i / n, y1 - hw) for i in range(1, n)],
        "left": [(x0 + hw, y0 + (y1 - y0) * i / n) for i in range(1, n)],
        "right": [(x1 - hw, y0 + (y1 - y0) * i / n) for i in range(1, n)],
    }
    hits = {}
    for name, pts in sides.items():
        count = 0
        for x, y in pts:
            x, y = int(round(x)), int(round(y))
            window = [
                img.getpixel((min(max(x + dx, 0), img.width - 1), min(max(y + dy, 0), img.height - 1)))
                for dx in (-1, 0, 1)
                for dy in (-1, 0, 1)
            ]
            if any(_near(w) for w in window):
                count += 1
        hits[name] = count
    return hits


def test_red_box_is_still_visible_after_resize_and_compression(tmp_path):
    box = (300, 200, 800, 500)
    p = _make_page(str(tmp_path / "a.png"), box=box)
    out = _decode(email_images.prepare(p))
    scale = out.width / 1280
    hits = _side_hits(out, box, scale)
    for side, count in hits.items():
        assert count >= 9, f"{side} side lost the box: {hits}"


def test_prepare_never_crops_the_box_out_of_the_frame(tmp_path):
    p = _make_page(str(tmp_path / "t.png"), size=(1280, 6000), box=(100, 5800, 600, 5990))
    out = _decode(email_images.prepare(p))
    assert out.height <= 1500
    assert abs(out.width / out.height - 1280 / 6000) < 0.01  # scaled, not cropped
    sc = out.height / 6000
    found = any(
        _near(out.getpixel((x, y)), 110)
        for y in range(int(5800 * sc), out.height)
        for x in range(out.width)
    )
    assert found


def test_prepare_never_upscales(tmp_path):
    p = _make_page(str(tmp_path / "s.png"), size=(400, 300))
    assert _decode(email_images.prepare(p)).width == 400


def test_prepare_all_uses_main_image_and_cids(tmp_path):
    a = _make_page(str(tmp_path / "a.png"))
    b = _make_page(str(tmp_path / "b.png"))
    shots = [
        {"path": a, "closeup_path": str(tmp_path / "missing_closeup.png")},
        {"path": b, "closeup_path": None},
    ]
    out = email_images.prepare_all(shots)
    assert [c for c, _ in out] == ["flaw1", "flaw2"]
    assert all(isinstance(d, bytes) for _, d in out)


# ---------------------------------------------------------------- renderer

def _copy(n=3, **over):
    c = {
        "subject": "Quick note about Acme's website",
        "headline": "Three things a visitor sees on your site",
        "opening": "I looked at your site and found a few things.",
        "sections": [
            {"index": i, "title": f"Title number {i + 1}", "sentence": f"Sentence number {i + 1}."}
            for i in range(n)
        ],
    }
    c.update(over)
    return c


def _shots(tmp_path, n=3):
    out = []
    for i in range(n):
        p = _make_page(str(tmp_path / f"s{i}.png"))
        out.append({"path": p, "closeup_path": None, "page": "/", "description": "d",
                    "impact": "serious", "selector": "x", "kind": "desktop"})
    return out


def _render(tmp_path, n=3, copy=None, first_name="Priya", company="Acme", reply_to="hello@mmga.agency"):
    return email_html.render(copy or _copy(n), _shots(tmp_path, n), first_name=first_name,
                             company=company, reply_to=reply_to)


def test_html_under_80kb_with_five_sections(tmp_path):
    html, _ = _render(tmp_path, 5)
    assert len(html.encode("utf-8")) < 80_000
    assert html.count("cid:flaw") == 5
    assert html.count("<!--FOOTER-->") == 1
    assert "Hi Priya," in html


def test_greeting_fallback(tmp_path):
    html, text = _render(tmp_path, first_name="")
    assert "Hi there," in html and "Hi there," in text


def test_every_img_has_alt_width_and_height(tmp_path):
    html, _ = _render(tmp_path, 3)
    imgs = re.findall(r"<img\b[^>]*>", html)
    assert len(imgs) == 3
    for n, tag in enumerate(imgs, 1):
        assert f'src="cid:flaw{n}"' in tag
        assert re.search(r'alt="[^"]+"', tag)
        assert 'width="520"' in tag
        assert re.search(r'height="\d+"', tag)
        assert 'style="display:block;border:0;"' in tag
    assert 'height="325"' in imgs[0]  # 1280x800 at 520 wide


def test_img_height_falls_back_when_file_unreadable(tmp_path):
    shots = _shots(tmp_path, 1)
    shots[0]["path"] = str(tmp_path / "gone.png")
    html, _ = email_html.render(_copy(1), shots, first_name="A", company="B", reply_to="x@y.com")
    assert 'height="325"' in html


def test_html_escapes_markup_in_company_and_names(tmp_path):
    nasty = '<script>alert("x")</script> & "q" é中'
    copy = _copy(1, headline=nasty, opening=nasty)
    copy["sections"][0]["title"] = nasty
    copy["sections"][0]["sentence"] = nasty
    html, _ = email_html.render(copy, _shots(tmp_path, 1), first_name=nasty, company=nasty,
                                reply_to='a"b@x.com')
    assert "<script" not in html
    assert "&lt;script&gt;" in html
    assert "&amp;" in html
    assert "é中" in html
    assert 'a"b' not in html


def test_no_tracking_pixel_and_no_remote_urls(tmp_path):
    html, _ = _render(tmp_path, 3)
    assert "http" not in html.lower()
    assert not re.search(r'(width|height)="1"', html)
    assert "<style" not in html and "<script" not in html and "class=" not in html
    for src in re.findall(r'src="([^"]*)"', html):
        assert src.startswith("cid:")
    low = html.lower()
    for bad in ("#ffffff", "#fff;", "#fff\"", "#000000", "#000;", "#000\""):
        assert bad not in low


def test_button_is_mailto_with_prefilled_subject(tmp_path):
    html, _ = _render(tmp_path)
    assert 'href="mailto:hello@mmga.agency?subject=Re%3A%20Quick%20note%20about%20Acme%27s%20website"' in html
    assert "Want the full review? Reply yes" in html


def test_button_without_reply_to_still_renders(tmp_path):
    html, text = _render(tmp_path, reply_to="")
    assert "Want the full review? Reply yes" in html
    assert "mailto:" not in html
    assert "Just reply to this email" in text


def test_automation_note_is_last_block_before_footer_marker(tmp_path):
    html, _ = _render(tmp_path)
    marker = html.index("<!--FOOTER-->")
    note = html.index("compliance, finance and operations")
    assert note < marker
    assert note < html.rindex("Want the full review") < marker  # button lives inside the closing panel
    assert "<img" not in html[note:marker]
    assert "<img" not in html[marker:]
    assert html.count("cid:flaw") == 3 and html.rindex("cid:flaw") < note


def test_text_version_contains_every_section_title_and_sentence(tmp_path):
    c = _copy(4)
    _, text = _render(tmp_path, 4, copy=c)
    for s in c["sections"]:
        assert f'{s["index"] + 1}. {s["title"]}' in text
        assert s["sentence"] in text
    assert c["headline"] in text and c["opening"] in text
    assert "Hi Priya," in text
    assert "screenshots are in the html version" in text.lower()
    assert "reply yes" in text.lower()


def test_copy_has_no_hyphens_or_dashes_in_template_text(tmp_path):
    empty = {"subject": "", "headline": "", "opening": "",
             "sections": [{"index": 0, "title": "", "sentence": ""}]}
    html, text = email_html.render(empty, _shots(tmp_path, 1), first_name="", company="", reply_to="")
    visible = re.sub(r"<[^>]+>", " ", html)
    for ch in ("-", "–", "—"):
        assert ch not in visible, repr(ch)
        assert ch not in text, repr(ch)


# ------------------------------------------------------------ review fixes

def test_render_rejects_count_mismatch_both_directions(tmp_path):
    with pytest.raises(ValueError):
        email_html.render(_copy(3), _shots(tmp_path, 2), first_name="A", company="B", reply_to="x@y.com")
    with pytest.raises(ValueError):
        email_html.render(_copy(2), _shots(tmp_path, 3), first_name="A", company="B", reply_to="x@y.com")


def test_render_rejects_zero_and_six_shots(tmp_path):
    with pytest.raises(ValueError):
        email_html.render(_copy(0), [], first_name="A", company="B", reply_to="x@y.com")
    with pytest.raises(ValueError):
        email_html.render(_copy(6), _shots(tmp_path, 6), first_name="A", company="B", reply_to="x@y.com")


def test_render_rejects_wrong_index_order(tmp_path):
    c = _copy(3)
    c["sections"][0], c["sections"][1] = c["sections"][1], c["sections"][0]
    with pytest.raises(ValueError):
        email_html.render(c, _shots(tmp_path, 3), first_name="A", company="B", reply_to="x@y.com")


def test_render_correct_input_renders(tmp_path):
    html, text = _render(tmp_path, 5)
    assert html.count("cid:flaw") == 5 and "5. Title number 5" in text


def test_prepare_noisy_image_falls_back_in_width_and_warns(tmp_path, capsys):
    import random
    rnd = random.Random(1)
    img = Image.frombytes("RGB", (1280, 800), rnd.randbytes(1280 * 800 * 3))
    p = str(tmp_path / "noise.png")
    img.save(p)
    data = email_images.prepare(p, max_bytes=20_000, require_box=False)
    assert Image.open(io.BytesIO(data)).width == 460 and len(data) <= 20_000  # width fallback
    assert capsys.readouterr().out == ""
    data = email_images.prepare(p, max_bytes=10_000, require_box=False)  # nothing fits: warn, return anyway
    out = capsys.readouterr().out
    assert len(data) > 10_000 and p in out and str(len(data)) in out


def test_prepare_transparency_is_composited_on_cream(tmp_path):
    for mode in ("RGBA", "LA"):
        img = Image.new(mode, (200, 100), (0, 0) if mode == "LA" else (0, 0, 0, 0))
        p = str(tmp_path / f"{mode}.png")
        img.save(p)
        px = _decode(email_images.prepare(p, require_box=False)).getpixel((100, 50))
        assert all(abs(a - b) <= 6 for a, b in zip(px, (255, 248, 239))), (mode, px)
    pal = Image.new("P", (200, 100), 0)
    pal.putpalette([0, 0, 0] * 256)
    p = str(tmp_path / "P.png")
    pal.save(p, transparency=0)
    px = _decode(email_images.prepare(p, require_box=False)).getpixel((100, 50))
    assert all(abs(a - b) <= 6 for a, b in zip(px, (255, 248, 239))), px


def test_blank_title_falls_back_to_screenshot_alt(tmp_path):
    c = _copy(2)
    c["sections"][0]["title"] = "   "
    c["sections"][1]["title"] = ""
    html, _ = _render(tmp_path, 2, copy=c)
    assert 'alt="Screenshot 1"' in html and 'alt="Screenshot 2"' in html


def test_reply_to_is_sanitized_for_mailto(tmp_path):
    html, _ = _render(tmp_path, reply_to="me@x.com?cc=y@z.com")
    assert html.count("mailto:") == 1
    assert 'href="mailto:me@x.com?subject=' in html
    assert "cc=" not in html
    html, text = _render(tmp_path, reply_to=" ?cc=a@b.com")
    assert "mailto:" not in html and "<a " not in html
    assert "Just reply to this email" in text


def test_button_cell_carries_padding_and_bgcolor(tmp_path):
    html, _ = _render(tmp_path)
    m = re.search(r'<td bgcolor="#d9480f"[^>]*>', html)
    assert m and "padding:13px 26px" in m.group(0)


def test_text_order_matches_html(tmp_path):
    c = _copy(2)
    _, text = _render(tmp_path, 2, copy=c)
    assert text.index(c["headline"]) < text.index("Hi Priya,") < text.index(c["opening"])


# ---- the box must survive the resize (fix wave I3) -------------------------
# Measured: the 4px box of a 1280px capture is about 1.9px at 600 wide and
# 1.6px at 520. The smallest box the capture draws (a 12px element plus the
# 4px padding, 20px square) leaves 8 to 16 strongly magenta pixels at 600 on a
# plain page; a 60x30 box leaves 64 to 178, and a 200x60 box over 200.


def test_a_visible_box_passes_and_is_counted(tmp_path):
    for box in (DEFAULT_BOX, (400, 300, 599, 359)):
        p = _make_page(str(tmp_path / "v.png"), box=box)
        data = email_images.prepare(p)
        assert email_images.magenta_pixel_count(data) >= email_images.MIN_BOX_PIXELS


def test_an_image_without_a_box_is_refused(tmp_path):
    p = _make_page(str(tmp_path / "n.png"), box=None)
    with pytest.raises(email_images.BoxNotVisibleError):
        email_images.prepare(p)
    with pytest.raises(email_images.BoxNotVisibleError):
        email_images.prepare_all([{"path": p}])


def test_a_box_too_tiny_to_see_after_the_resize_is_refused(tmp_path):
    p = _make_page(str(tmp_path / "t.png"), box=(400, 300, 419, 319))  # the smallest box drawn
    with pytest.raises(email_images.BoxNotVisibleError):
        email_images.prepare(p)


def test_a_thin_box_on_a_huge_page_is_refused(tmp_path):
    # 1280x6000 scales to a quarter: a 4px border becomes 1px, a small box vanishes.
    p = _make_page(str(tmp_path / "h.png"), size=(1280, 6000), box=(400, 300, 439, 339))
    with pytest.raises(email_images.BoxNotVisibleError):
        email_images.prepare(p)


def test_box_not_visible_error_is_a_value_error():
    assert issubclass(email_images.BoxNotVisibleError, ValueError)
