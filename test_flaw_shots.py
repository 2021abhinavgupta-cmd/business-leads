"""collect_flaw_shots: one boxed screenshot per measured flaw, no browser."""
import os
from io import BytesIO

from PIL import Image

from analyzer import visuals
from analyzer.visuals import collect_flaw_shots, make_flaw_shot_filename


def _png(color=(200, 30, 30)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (8, 6), color).save(buf, format="PNG")
    return buf.getvalue()


def _flaw(desc="Low contrast text", impact="serious", selector="#hero h1"):
    return {"description": desc, "impact": impact, "selector": selector}


def _page(label="Homepage", flaw=None, closeup=True):
    return {
        "label": label,
        "screenshot_bytes": _png(),
        "closeup_bytes": _png((10, 10, 200)) if closeup else None,
        "visual_flaw": flaw,
    }


def _collect(pages, tmp_path, mobile_flaw=None, mobile_bytes=None, mobile_closeup=None, **kw):
    return collect_flaw_shots(
        pages, mobile_flaw, mobile_bytes, "Acme Co", "acme.com",
        mobile_closeup_bytes=mobile_closeup, out_dir=str(tmp_path), **kw,
    )


def test_one_shot_per_page_that_has_a_visual_flaw(tmp_path):
    pages = [
        _page("Homepage", _flaw("A")),
        _page("About", _flaw("B")),
    ]
    shots = _collect(pages, tmp_path)
    assert len(shots) == 2
    assert {s["page"] for s in shots} == {"Homepage", "About"}
    assert all(s["kind"] == "desktop" for s in shots)


def test_pages_without_a_box_are_skipped(tmp_path):
    pages = [_page("Homepage", None), _page("About", _flaw("B"))]
    shots = _collect(pages, tmp_path)
    assert [s["page"] for s in shots] == ["About"]


def test_same_description_on_two_pages_gives_one_shot(tmp_path):
    pages = [
        _page("Homepage", _flaw("Same", impact="minor")),
        _page("About", _flaw("Same", impact="critical")),
    ]
    shots = _collect(pages, tmp_path)
    assert len(shots) == 1
    assert shots[0]["page"] == "About"
    assert shots[0]["impact"] == "critical"


def test_mobile_shot_included_only_when_a_mobile_box_was_drawn(tmp_path):
    assert _collect([], tmp_path, mobile_flaw=None, mobile_bytes=_png()) == []
    assert _collect([], tmp_path, mobile_flaw=_flaw("M"), mobile_bytes=None) == []
    shots = _collect([], tmp_path, mobile_flaw=_flaw("M"), mobile_bytes=_png())
    assert len(shots) == 1
    assert shots[0]["kind"] == "mobile"
    assert shots[0]["closeup_path"] is None


def test_mobile_shot_carries_its_closeup_when_one_was_captured(tmp_path):
    shots = _collect(
        [], tmp_path, mobile_flaw=_flaw("M"), mobile_bytes=_png(), mobile_closeup=_png()
    )
    assert shots[0]["closeup_path"] and os.path.exists(shots[0]["closeup_path"])


def test_every_shot_carries_the_selector(tmp_path):
    pages = [_page("Homepage", _flaw("A", selector="#a"))]
    shots = _collect(pages, tmp_path, mobile_flaw=_flaw("M", selector=".m"), mobile_bytes=_png())
    assert sorted(s["selector"] for s in shots) == ["#a", ".m"]
    assert all(s["description"] and s["impact"] for s in shots)


def test_shots_sorted_most_severe_first_and_capped_at_max_shots(tmp_path):
    impacts = ["minor", "critical", "moderate", "serious"]
    pages = [_page(f"P{i}", _flaw(f"D{i}", impact=imp)) for i, imp in enumerate(impacts)]
    shots = _collect(pages, tmp_path, max_shots=3)
    assert [s["impact"] for s in shots] == ["critical", "serious", "moderate"]


def test_files_are_jpeg_and_exist(tmp_path):
    shots = _collect([_page("Homepage", _flaw("A"))], tmp_path)
    for p in (shots[0]["path"], shots[0]["closeup_path"]):
        assert os.path.exists(p)
        assert p.startswith(str(tmp_path))
        with Image.open(p) as im:
            assert im.format == "JPEG"


def test_filenames_are_distinct_per_index_and_kind():
    names = {
        make_flaw_shot_filename("Acme Co", "acme.com", 0, "desktop"),
        make_flaw_shot_filename("Acme Co", "acme.com", 1, "desktop"),
        make_flaw_shot_filename("Acme Co", "acme.com", 0, "mobile"),
        make_flaw_shot_filename("Acme Co", "acme.com", 0, "desktop_closeup"),
        visuals.make_screenshot_filename("Acme Co", "acme.com"),
    }
    assert len(names) == 5
    assert all(n.endswith(".jpg") for n in names)
