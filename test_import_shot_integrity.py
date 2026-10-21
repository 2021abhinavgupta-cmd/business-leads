"""Fix wave C1: an import lead's screenshots are its own, and are checked by hash.

Two leads for the same company and domain, or a Dashboard audit of the same
site, must never overwrite each other's flaw shots, and a shot file that
changed on disk after the audit must stop the draft, the preview and the send.
"""
import hashlib
import os
from io import BytesIO

import pytest
from PIL import Image, ImageDraw

import app as app_module
from analyzer import visuals
from importer import audit_runner, drafting, service
from import_test_support import client_with_key, flaw_copy, make_row, seed_designed_lead, use_temp_db
from storage import import_store


@pytest.fixture(autouse=True)
def _db(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    monkeypatch.setattr(audit_runner, "_check_url", lambda url: None)
    audit_runner._reset_for_tests()
    yield
    audit_runner._reset_for_tests()


def _boxed_png(color) -> bytes:
    img = Image.new("RGB", (1280, 800), color)
    ImageDraw.Draw(img).rectangle((300, 200, 700, 400), outline=(255, 0, 255), width=6)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _pages(color):
    return [
        {"label": "/", "screenshot_bytes": _boxed_png(color), "closeup_bytes": None,
         "visual_flaw": {"description": f"defect {i}", "impact": "serious", "selector": f"#e{i}"}}
        for i in range(2)
    ]


def _writing_capture(out_dir):
    """A capture that really writes its shots, in a new color on every call."""
    calls = []

    async def capture(url, company_name, on_queued=None, on_started=None, *, file_prefix="", **_kw):
        calls.append(file_prefix)
        shots = visuals.collect_flaw_shots(_pages((40 * len(calls), 120, 160)), None, None,
                                           company_name, url, out_dir=str(out_dir),
                                           file_prefix=file_prefix)
        return "p.jpg", "<html/>", {"flaw_shots": shots}
    capture.calls = calls
    return capture


def _yes(image, description):
    return True


class _Auditor:
    def generate_flaw_email_copy(self, lead, homepage_text, shots, your_name, feedback=None):
        return flaw_copy(len(shots))

    def generate_import_email(self, *a, **k):  # must never be reached here
        raise AssertionError("fell back to the text email")


def _draft(lead):
    return drafting.draft_designed(lead, _Auditor(), your_name="K", reply_to="r@x.example",
                                   fetch=lambda d: "Acme Bank serves Boston.")


def _same_company_leads(n=2):
    rows = [make_row(email=f"p{i}@acme.com", status="ready") for i in range(n)]
    batch = import_store.create_batch("same.csv", rows)
    return [lead["id"] for lead in import_store.list_leads(batch)["leads"]]


async def _audit(ids, capture):
    assert audit_runner.start_audit(ids, capture=capture, judge=_yes) is True
    await audit_runner._task


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


async def test_audit_names_files_per_lead_and_stores_each_files_hash(tmp_path):
    ids = _same_company_leads(1)
    cap = _writing_capture(tmp_path / "shots")
    await _audit(ids, cap)
    assert cap.calls == [visuals.import_shot_prefix(ids[0])]
    lead = import_store.get_lead(ids[0])
    assert lead["audit_status"] == "done" and len(lead["flaw_shots"]) == 2
    for s in lead["flaw_shots"]:
        assert os.path.basename(s["path"]).startswith(visuals.import_shot_prefix(ids[0]))
        assert s["sha256"] == _sha(s["path"])


async def test_second_lead_with_the_same_company_and_domain_leaves_the_first_intact(tmp_path):
    first, second = _same_company_leads(2)
    cap = _writing_capture(tmp_path / "shots")
    await _audit([first], cap)
    before = import_store.get_lead(first)["flaw_shots"]
    await _audit([second], cap)
    other = import_store.get_lead(second)["flaw_shots"]
    assert not {s["path"] for s in before} & {s["path"] for s in other}
    lead = import_store.get_lead(first)
    assert all(_sha(s["path"]) == s["sha256"] for s in lead["flaw_shots"])
    assert drafting.is_designable(lead) is True
    assert _draft(lead)["html_body"]


async def test_a_dashboard_capture_of_the_same_site_cannot_touch_an_import_leads_files(tmp_path):
    (lead_id,) = _same_company_leads(1)
    out_dir = tmp_path / "shots"
    await _audit([lead_id], _writing_capture(out_dir))
    lead = import_store.get_lead(lead_id)
    # The Dashboard passes no prefix: same company name and URL as the import audit.
    dash = visuals.collect_flaw_shots(_pages((1, 2, 3)), None, None, lead["organization"],
                                      f"https://{lead['domain']}", out_dir=str(out_dir))
    assert len(dash) == 2
    assert not {s["path"] for s in dash} & {s["path"] for s in lead["flaw_shots"]}
    assert drafting.is_designable(import_store.get_lead(lead_id)) is True


async def _tampered_lead(tmp_path):
    (lead_id,) = _same_company_leads(1)
    await _audit([lead_id], _writing_capture(tmp_path / "shots"))
    path = import_store.get_lead(lead_id)["flaw_shots"][0]["path"]
    Image.new("RGB", (1280, 800), (9, 9, 9)).save(path, format="JPEG")
    return lead_id


async def test_a_shot_changed_on_disk_after_the_audit_refuses_the_draft(tmp_path):
    lead_id = await _tampered_lead(tmp_path)
    lead = import_store.get_lead(lead_id)
    assert drafting.shots_intact(lead["flaw_shots"]) is False
    assert drafting.is_designable(lead) is False
    with pytest.raises(drafting.ShotsChangedError):
        _draft(lead)


async def test_the_draft_route_answers_409_for_a_shot_changed_on_disk(monkeypatch, tmp_path):
    lead_id = await _tampered_lead(tmp_path)
    client = client_with_key(monkeypatch)
    monkeypatch.setattr(app_module, "auditor", _Auditor())
    r = client.post(f"/api/import/leads/{lead_id}/draft")
    assert r.status_code == 409, r.text
    assert "re-audit" in r.json()["detail"].lower()
    assert import_store.get_lead(lead_id)["status"] == "ready"


def _fingerprint_after_tamper(tmp_path, status):
    """A designed lead drafted AFTER its first shot was overwritten: the prepared
    byte fingerprints match the disk, only the audit time hash can tell."""
    lead_id = seed_designed_lead(tmp_path, status=status)
    lead = import_store.get_lead(lead_id)
    img = Image.new("RGB", (900, 600), (9, 9, 9))
    ImageDraw.Draw(img).rectangle((100, 100, 500, 300), outline=(255, 0, 255), width=6)
    img.save(lead["flaw_shots"][0]["path"])
    design = {**lead["design_json"], "shot_fingerprints": drafting.shot_fingerprints(lead["flaw_shots"])}
    import_store.update_lead(lead_id, design_json=design)
    return lead_id


def test_shots_match_design_fails_when_the_file_no_longer_has_its_audit_hash(tmp_path):
    lead_id = _fingerprint_after_tamper(tmp_path, "drafted")
    assert drafting.shots_match_design(import_store.get_lead(lead_id)) is False
    with pytest.raises(service.FlowError) as exc:
        service.preview_html(lead_id)
    assert exc.value.status_code == 409 and "re-audit" in str(exc.value.detail).lower()


def test_send_check_answers_409_when_the_file_no_longer_has_its_audit_hash(tmp_path):
    lead_id = _fingerprint_after_tamper(tmp_path, "approved")
    with pytest.raises(app_module.HTTPException) as exc:
        app_module._check_designed_import(import_store.get_lead(lead_id))
    assert exc.value.status_code == 409 and "re-audit" in str(exc.value.detail).lower()


def test_old_shot_entries_without_a_hash_fail_closed(tmp_path):
    lead_id = seed_designed_lead(tmp_path)
    lead = import_store.get_lead(lead_id)
    shots = [{k: v for k, v in s.items() if k != "sha256"} for s in lead["flaw_shots"]]
    old = {**lead, "flaw_shots": shots}
    assert drafting.shots_intact(shots) is False
    assert drafting.is_designable(old) is False
    assert drafting.shots_match_design(old) is False
    with pytest.raises(app_module.HTTPException) as exc:
        app_module._check_designed_import({**old, "status": "approved"})
    assert exc.value.status_code == 409
