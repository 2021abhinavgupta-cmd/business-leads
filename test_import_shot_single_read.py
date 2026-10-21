"""Fix wave 2 item 6: each shot file is read once; the bytes that were hashed are
the bytes that get prepared, fingerprinted and sent. A file swapped between the
hash check and the prepare can no longer slip in."""
import hashlib

import pytest
from PIL import Image, ImageDraw

import app as app_module
from importer import drafting, email_images
from import_test_support import flaw_copy, make_flaw_shots
from storage import import_store
from test_import_drafting import HOMEPAGE, LEAD, _FlawAuditor
from test_import_send import _designed as _approved_designed, _post, designed_env, env  # noqa: F401


def _other_image(path):
    img = Image.new("RGB", (900, 600), (20, 20, 120))
    ImageDraw.Draw(img).rectangle((100, 100, 500, 300), outline=(255, 0, 255), width=6)
    img.save(path, format="PNG")


def test_prepare_bytes_matches_prepare_from_the_file(tmp_path):
    (shot,) = make_flaw_shots(tmp_path, 1)
    raw = open(shot["path"], "rb").read()
    assert email_images.prepare_bytes(raw, name=shot["path"]) == email_images.prepare(shot["path"])


def test_read_intact_returns_the_hashed_bytes_or_none(tmp_path):
    shots = make_flaw_shots(tmp_path, 2)
    raw = drafting.read_intact(shots)
    assert [hashlib.sha256(b).hexdigest() for b in raw] == [s["sha256"] for s in shots]
    _other_image(shots[1]["path"])
    assert drafting.read_intact(shots) is None
    assert drafting.read_intact([]) is None
    assert drafting.read_intact([{**shots[0], "sha256": None}]) is None


def test_prepare_all_uses_the_given_bytes_not_the_file(tmp_path):
    shots = make_flaw_shots(tmp_path, 2)
    expected = email_images.prepare_all(shots)
    raw = drafting.read_intact(shots)
    for s in shots:
        _other_image(s["path"])
    assert email_images.prepare_all(shots, raw) == expected


def _swap_after_read(monkeypatch, shots):
    real = drafting.read_intact

    def read_then_swap(s):
        out = real(s)
        for shot in shots:
            _other_image(shot["path"])
        return out
    monkeypatch.setattr(drafting, "read_intact", read_then_swap)


def test_draft_fingerprints_the_bytes_it_checked(tmp_path, monkeypatch):
    shots = make_flaw_shots(tmp_path, 2)
    expected = drafting.shot_fingerprints(shots)
    lead = {**LEAD, "id": 1, "audit_status": "done", "flaw_shots": shots}
    _swap_after_read(monkeypatch, shots)
    out = drafting.draft_designed(lead, _FlawAuditor([flaw_copy(2)]), your_name="Kshitij Gupta",
                                  reply_to="reply@mmga.example", fetch=lambda d: HOMEPAGE)
    assert out["html_body"] and out["design"]["shot_fingerprints"] == expected


def test_send_attaches_the_bytes_it_checked(designed_env, tmp_path, monkeypatch):
    client, sender = designed_env
    lead_id = _approved_designed(tmp_path)
    shots = import_store.get_lead(lead_id)["flaw_shots"]
    expected = email_images.prepare_all(shots)
    _swap_after_read(monkeypatch, shots)
    r = _post(client, lead_id)
    assert r.status_code == 200, r.text
    assert sender.designed_calls[0]["images"] == expected


def test_send_still_refuses_files_changed_before_the_read(designed_env, tmp_path):
    client, sender = designed_env
    lead_id = _approved_designed(tmp_path)
    _other_image(import_store.get_lead(lead_id)["flaw_shots"][0]["path"])
    r = _post(client, lead_id)
    assert r.status_code == 409 and r.json()["detail"] == drafting.SHOTS_CHANGED_ON_DISK
    assert sender.designed_calls == []
