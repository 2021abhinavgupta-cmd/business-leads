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


def test_corrupt_screenshot_skips_only_that_shot(tmp_path):
    bad = _page("Bad", _flaw("A", impact="critical"))
    bad["screenshot_bytes"] = b"not an image"
    good = _page("Good", _flaw("B", impact="minor"))
    shots = _collect([bad, good], tmp_path)
    assert [s["page"] for s in shots] == ["Good"]
    assert not any("_flaw0_" in f for f in os.listdir(tmp_path))


def test_failing_save_degrades_to_empty_list_without_partial_files(tmp_path, monkeypatch):
    def boom(img, path):
        raise OSError("disk full")
    monkeypatch.setattr(visuals, "_save_jpeg", boom)
    assert _collect([_page("Homepage", _flaw("A"))], tmp_path) == []
    assert os.listdir(tmp_path) == []


def test_safe_wrapper_returns_empty_list_when_collection_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(visuals, "collect_flaw_shots", boom)
    assert visuals._safe_collect_flaw_shots([], None, None, "Acme", "acme.com") == []


def test_mobile_duplicate_of_desktop_tie_keeps_desktop(tmp_path):
    shots = _collect(
        [_page("Homepage", _flaw("X", impact="serious"))], tmp_path,
        mobile_flaw=_flaw("X", impact="serious"), mobile_bytes=_png(),
    )
    assert [s["kind"] for s in shots] == ["desktop"]


def test_mobile_duplicate_with_higher_impact_replaces_desktop(tmp_path):
    shots = _collect(
        [_page("Homepage", _flaw("X", impact="minor"))], tmp_path,
        mobile_flaw=_flaw("X", impact="critical"), mobile_bytes=_png(),
    )
    assert [s["kind"] for s in shots] == ["mobile"]


# --- per-lead file names for import audits (fix wave C1) ---

def test_file_prefix_names_every_saved_file(tmp_path):
    prefix = visuals.import_shot_prefix(7)
    shots = _collect([_page("Homepage", _flaw("A"))], tmp_path, file_prefix=prefix)
    for p in (shots[0]["path"], shots[0]["closeup_path"]):
        assert os.path.basename(p).startswith(prefix)


def test_two_import_leads_with_the_same_company_and_url_never_share_a_file(tmp_path):
    a = _collect([_page("Homepage", _flaw("A"))], tmp_path, file_prefix=visuals.import_shot_prefix(1))
    b = _collect([_page("Homepage", _flaw("A"))], tmp_path, file_prefix=visuals.import_shot_prefix(2))
    assert a[0]["path"] != b[0]["path"] and a[0]["closeup_path"] != b[0]["closeup_path"]


def test_dashboard_names_can_never_equal_import_names():
    # Dashboard names are slug + hex hash, and the slug turns every non
    # alphanumeric character into "_", so they never contain "-"; the import
    # prefix always does (and ends in "-", so lead 1 is never a prefix of lead 11).
    for company in ("import-lead1-acme", "Acme-Co", "import lead1 acme", "x"):
        for kind in ("desktop", "mobile", "desktop_closeup"):
            assert "-" not in make_flaw_shot_filename(company, "acme.com", 0, kind)
            assert "-" not in visuals.make_screenshot_filename(company, "acme.com")
    assert visuals.import_shot_prefix(1) == "import-lead1-"
    assert not visuals.import_shot_prefix(11).startswith(visuals.import_shot_prefix(1))
