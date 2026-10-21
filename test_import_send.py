import hashlib
import os
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

import app as app_module
from importer import email_images, send_policy
from storage import db, import_store
from import_test_support import client_with_key, empty_blockers, seed_designed_lead, seed_lead, use_temp_db

OK_BOUNCE = {"paused": False}


def _lead(**over):
    lead = {
        "id": 1, "status": "approved", "email": "jane@acme.com", "domain": "acme.com",
        "country": "united states", "review_warnings": [], "subject": "S", "body": "B",
    }
    lead.update(over)
    return lead


def _check(lead, **over):
    kwargs = dict(
        req_email="jane@acme.com", acknowledge_warnings=False, mailing_address="1 Road",
        blockers=empty_blockers(), bounce=OK_BOUNCE,
    )
    kwargs.update(over)
    return send_policy.check_import_send(lead, **kwargs)


# ---- policy ----------------------------------------------------------------

def test_a_valid_approved_lead_passes():
    assert _check(_lead()) is None


@pytest.mark.parametrize("lead,over,code", [
    (None, {}, 404),
    (_lead(status="drafted"), {}, 409),
    (_lead(), {"req_email": "other@acme.com"}, 409),
    (_lead(), {"mailing_address": ""}, 409),
    (_lead(country="india"), {"mailing_address": ""}, None),
    (_lead(), {"bounce": {"paused": True, "sent": 30, "bounced": 4, "rate_pct": 13.33, "pause_pct": 5}}, 409),
    (_lead(review_warnings=["w"]), {}, 409),
    (_lead(review_warnings=["w"]), {"acknowledge_warnings": True}, None),
])
def test_policy_gates(lead, over, code):
    if code is None:
        assert _check(lead, **over) is None
        return
    with pytest.raises(send_policy.SendBlocked) as exc:
        _check(lead, **over)
    assert exc.value.status_code == code


def test_state_that_changed_since_approval_blocks_with_a_skip_reason():
    with pytest.raises(send_policy.SendBlocked) as exc:
        _check(_lead(), blockers=empty_blockers(suppressed={"jane@acme.com"}))
    assert exc.value.skip_reason == "suppressed"


def test_the_us_gate_names_the_setting():
    with pytest.raises(send_policy.SendBlocked) as exc:
        _check(_lead(), mailing_address="")
    assert "MAILING_ADDRESS" in str(exc.value.detail)


def test_bounce_pause_boundaries():
    rows = lambda sent, bounced: [{"variant": "import-automation", "sent": sent, "bounced": bounced}]
    assert send_policy.bounce_status([], 20, 5)["paused"] is False
    assert send_policy.bounce_status(rows(19, 19), 20, 5)["paused"] is False   # below the sample floor
    assert send_policy.bounce_status(rows(20, 1), 20, 5)["paused"] is False    # exactly 5 percent
    assert send_policy.bounce_status(rows(20, 2), 20, 5)["paused"] is True     # 10 percent
    other = [{"variant": "short", "sent": 100, "bounced": 100}]
    assert send_policy.bounce_status(other, 20, 5)["sent"] == 0                # only this feature counts


# ---- /api/send -------------------------------------------------------------

class _Sender:
    def __init__(self, result="<sent@x.com>", error=None):
        self.calls, self.result, self.error = [], result, error
        self.designed_calls = []

    def send_email(self, to_email, subject, body, **kw):
        self.calls.append({"to": to_email, "subject": subject, "body": body, **kw})
        if self.error:
            raise self.error
        return self.result

    def send_designed_email(self, to_email, subject, text_body, html_body, images):
        self.designed_calls.append({
            "to": to_email, "subject": subject, "body": text_body,
            "html_body": html_body, "images": images,
        })
        if self.error:
            raise self.error
        return self.result


def _approved(**over):
    return seed_lead(status="approved", subject="Stored subject", body="Stored body", **over)


def _post(client, lead_id, **over):
    payload = {
        "email": "jane@acme.com", "subject": "CLIENT subject", "body": "CLIENT body",
        "company": "Acme Bank", "website": "acme.com", "import_lead_id": lead_id,
        "acknowledge_warnings": False,
    }
    payload.update(over)
    return client.post("/api/send", json=payload)


@pytest.fixture
def env(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    monkeypatch.setattr(app_module.config, "MAILING_ADDRESS", "1 Example Road, Mumbai")
    monkeypatch.setattr(app_module.config, "DAILY_EMAIL_LIMIT", 50)
    sender = _Sender()
    monkeypatch.setattr(app_module, "ses", sender)
    return client_with_key(monkeypatch), sender


def test_sends_the_stored_copy_not_the_clients(env):
    client, sender = env
    lead_id = _approved()
    r = _post(client, lead_id)
    assert r.status_code == 200, r.text
    assert sender.calls[0]["subject"] == "Stored subject" and sender.calls[0]["body"] == "Stored body"


def test_marks_sent_links_history_and_records_the_variant(env):
    client, _ = env
    lead_id = _approved()
    assert _post(client, lead_id).status_code == 200
    lead = import_store.get_lead(lead_id)
    assert lead["status"] == "sent" and lead["sent_at"] and lead["history_id"]
    row = db.get_email_history_by_id(lead["history_id"])
    assert row["variant"] == "import-automation" and row["website"] == "acme.com"
    assert row["subject"] == "Stored subject"


def test_an_import_send_never_attaches_screenshots_or_touches_audit_drafts(env, monkeypatch, tmp_path):
    from analyzer.visuals import (
        make_closeup_screenshot_filename, make_mobile_screenshot_filename, make_screenshot_filename,
    )
    client, sender = env
    shots = tmp_path / "shots"
    shots.mkdir()
    monkeypatch.setattr(app_module, "SCREENSHOTS_DIR", str(shots))
    for fn in (make_screenshot_filename, make_mobile_screenshot_filename, make_closeup_screenshot_filename):
        (shots / fn("Acme Bank", "acme.com")).write_bytes(b"png")
    # db.log_draft(company, website, target_email, subject, body, image_url, review_warnings=...):
    # an audit draft with an unacknowledged warning for the SAME website must not gate or be deleted.
    db.log_draft("Acme Bank", "acme.com", "jane@acme.com", "Audit subject", "Audit body", "img",
                 review_warnings=["x"])
    lead_id = _approved()
    r = _post(client, lead_id)
    assert r.status_code == 200, r.text
    call = sender.calls[0]
    assert not call.get("image_path") and not call.get("mobile_image_path") and not call.get("closeup_image_path")
    assert db.get_draft_by_website("acme.com") is not None


def test_identity_comes_from_the_stored_lead_not_the_client(env):
    client, sender = env
    lead_id = _approved()
    r = _post(client, lead_id, email=" Jane@Acme.com ", company="Wrong Co", website="wrong.com")
    assert r.status_code == 200, r.text
    assert sender.calls[0]["to"] == "jane@acme.com"
    lead = import_store.get_lead(lead_id)
    row = db.get_email_history_by_id(lead["history_id"])
    assert row["website"] == "acme.com" and row["company"] == "Acme Bank"


def test_a_lead_changed_between_the_gates_and_the_claim_is_not_sent(env, monkeypatch):
    client, sender = env
    lead_id = _approved()
    real = app_module._prepare_import_send

    async def flip_after_gates(req):
        lead = await real(req)
        import_store.update_lead(lead_id, status="drafted")
        return lead

    monkeypatch.setattr(app_module, "_prepare_import_send", flip_after_gates)
    r = _post(client, lead_id)
    assert r.status_code == 409 and "nothing was sent" in r.text
    assert sender.calls == []
    assert import_store.get_lead(lead_id)["status"] == "drafted"


def test_a_double_post_sends_exactly_once(env):
    client, sender = env
    lead_id = _approved()
    assert _post(client, lead_id).status_code == 200
    assert _post(client, lead_id).status_code == 409
    assert len(sender.calls) == 1


def test_a_db_failure_after_a_successful_send_cannot_lead_to_a_resend(env, monkeypatch):
    client, sender = env
    lead_id = _approved()

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(app_module.db, "log_email", boom)
    assert _post(client, lead_id).status_code == 500
    assert import_store.get_lead(lead_id)["status"] == "sent"
    assert len(sender.calls) == 1


def test_unapproved_missing_and_mismatched_leads_are_refused(env):
    client, sender = env
    drafted = seed_lead(status="drafted", subject="S", body="B")
    assert _post(client, drafted).status_code == 409
    assert _post(client, 99999).status_code == 404
    lead_id = _approved()
    assert _post(client, lead_id, email="someone@else.com").status_code == 409
    assert sender.calls == []


def test_us_lead_is_blocked_without_a_mailing_address(env, monkeypatch):
    client, sender = env
    monkeypatch.setattr(app_module.config, "MAILING_ADDRESS", "")
    lead_id = _approved()
    r = _post(client, lead_id)
    assert r.status_code == 409 and "MAILING_ADDRESS" in r.text
    assert sender.calls == []


def test_warnings_need_acknowledgement(env):
    client, _ = env
    lead_id = _approved(review_warnings=["w"])
    assert _post(client, lead_id).status_code == 409
    assert _post(client, lead_id, acknowledge_warnings=True).status_code == 200


def test_the_daily_cap_is_still_enforced(env, monkeypatch):
    client, sender = env
    monkeypatch.setattr(app_module.db, "count_emails_sent_today", lambda: 50)
    r = _post(client, _approved())
    assert r.status_code == 429 and sender.calls == []


def test_a_suppressed_recipient_is_marked_skipped(env):
    client, sender = env
    sender.result = False   # what the sender returns for a suppressed address
    lead_id = _approved()
    r = _post(client, lead_id)
    assert r.status_code == 400
    lead = import_store.get_lead(lead_id)
    assert lead["status"] == "skipped" and lead["skip_reason"] == "suppressed"


def test_a_transport_error_marks_the_lead_failed_with_the_real_error(env, monkeypatch):
    client, _ = env
    monkeypatch.setattr(app_module, "ses", _Sender(error=Exception("SES rejected: bad domain")))
    lead_id = _approved()
    r = _post(client, lead_id)
    assert r.status_code == 500
    lead = import_store.get_lead(lead_id)
    assert lead["status"] == "failed" and "SES rejected" in lead["error"]


def test_state_that_changed_after_approval_is_caught_at_send_time(env):
    client, sender = env
    lead_id = _approved()
    db.add_suppression("jane@acme.com", "unsub")
    r = _post(client, lead_id)
    assert r.status_code == 409 and sender.calls == []
    assert import_store.get_lead(lead_id)["skip_reason"] == "suppressed"


def test_the_bounce_pause_blocks_further_sends(env, monkeypatch):
    client, sender = env
    monkeypatch.setattr(
        app_module.db, "get_variant_performance",
        lambda: [{"variant": "import-automation", "sent": 20, "bounced": 2}],
    )
    r = _post(client, _approved())
    assert r.status_code == 409 and r.json()["detail"]["code"] == "import_paused"
    assert sender.calls == []


def test_existing_callers_without_import_lead_id_are_unchanged(env):
    client, sender = env
    db.log_draft("Y", "y.com", "x@y.com", "Draft subject", "Draft body", "img")
    r = client.post("/api/send", json={
        "email": "x@y.com", "subject": "Plain subject", "body": "Plain body",
        "company": "Y", "website": "y.com",
    })
    assert r.status_code == 200
    assert sender.calls[0]["subject"] == "Plain subject"
    assert db.get_draft_by_website("y.com") is None
    assert db.get_email_history()[0]["variant"] == app_module.config.EMAIL_VARIANT


def test_status_route(env, monkeypatch):
    client, _ = env
    monkeypatch.setattr(app_module.db, "count_emails_sent_today", lambda: 12)
    r = client.get("/api/import/status")
    body = r.json()
    assert body["sent_today"] == 12 and body["daily_limit"] == 50 and body["remaining"] == 38
    assert body["mailing_address_set"] is True and body["bounce"]["paused"] is False


# ---- /api/send for designed leads -----------------------------------------

def _designed(tmp_path, n=2, sub="shots", **over):
    """An approved designed lead whose shot files live in their own folder."""
    folder = tmp_path / sub
    folder.mkdir(exist_ok=True)
    return seed_designed_lead(folder, n, status="approved", **over)


def _utc_days_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def _assert_not_claimed(lead_id, sender):
    lead = import_store.get_lead(lead_id)
    assert lead["status"] == "approved" and not lead["sent_at"] and not lead["history_id"]
    assert sender.calls == [] and sender.designed_calls == []


@pytest.fixture
def designed_env(env, monkeypatch):
    monkeypatch.setattr(app_module.config, "DRAFT_STALE_DAYS", 7)
    return env


def test_designed_lead_uses_send_designed_email_with_prepared_images(designed_env, tmp_path):
    client, sender = designed_env
    lead_id = _designed(tmp_path)
    lead = import_store.get_lead(lead_id)
    r = _post(client, lead_id)
    assert r.status_code == 200, r.text
    assert sender.calls == [] and len(sender.designed_calls) == 1
    call = sender.designed_calls[0]
    assert call["to"] == "jane@acme.com"
    assert call["subject"] == lead["subject"] and call["body"] == lead["body"]
    assert call["html_body"] == lead["html_body"]
    assert call["images"] == email_images.prepare_all(lead["flaw_shots"])
    assert [cid for cid, _ in call["images"]] == ["flaw1", "flaw2"]


def test_text_lead_still_uses_send_email(designed_env):
    client, sender = designed_env
    r = _post(client, _approved())
    assert r.status_code == 200, r.text
    assert len(sender.calls) == 1 and sender.designed_calls == []
    assert sender.calls[0]["body"] == "Stored body"


def test_missing_screenshot_blocks_before_claiming_the_lead(designed_env, tmp_path):
    client, sender = designed_env
    lead_id = _designed(tmp_path)
    os.remove(import_store.get_lead(lead_id)["flaw_shots"][1]["path"])
    r = _post(client, lead_id)
    assert r.status_code == 409 and "missing on disk" in r.text
    _assert_not_claimed(lead_id, sender)


def test_send_blocked_when_shots_no_longer_match_the_design(designed_env, tmp_path):
    client, sender = designed_env
    lead_id = _designed(tmp_path)
    shots = import_store.get_lead(lead_id)["flaw_shots"]
    shots[0]["description"] = "A different flaw found by a later audit"
    import_store.update_lead(lead_id, flaw_shots=shots)
    r = _post(client, lead_id)
    assert r.status_code == 409 and "screenshots changed" in r.text
    _assert_not_claimed(lead_id, sender)


def test_stale_design_after_shot_file_overwritten_blocks_send(designed_env, tmp_path):
    client, sender = designed_env
    lead_id = _designed(tmp_path)
    path = import_store.get_lead(lead_id)["flaw_shots"][0]["path"]
    Image.new("RGB", (900, 600), (10, 90, 30)).save(path)   # same path, different pixels
    r = _post(client, lead_id)
    assert r.status_code == 409 and "screenshots changed" in r.text
    _assert_not_claimed(lead_id, sender)


def test_missing_stored_fingerprints_block_the_send(designed_env, tmp_path):
    client, sender = designed_env
    lead_id = _designed(tmp_path)
    design = import_store.get_lead(lead_id)["design_json"]
    design.pop("shot_fingerprints")
    import_store.update_lead(lead_id, design_json=design)
    r = _post(client, lead_id)
    assert r.status_code == 409 and "screenshots changed" in r.text
    _assert_not_claimed(lead_id, sender)


@pytest.mark.parametrize("edit", [
    lambda html: html.replace("cid:flaw2", "cid:flaw9"),          # reference without an image
    lambda html: html.replace('src="cid:flaw2"', 'src="x.png"'),  # image without a reference
    lambda html: html + '<img src="cid:flaw3">',                  # extra reference
])
def test_html_and_images_cid_mismatch_blocks_before_claiming(designed_env, tmp_path, edit):
    client, sender = designed_env
    lead_id = _designed(tmp_path)
    html = import_store.get_lead(lead_id)["html_body"]
    changed = edit(html)
    assert changed != html
    import_store.update_lead(lead_id, html_body=changed)
    r = _post(client, lead_id)
    assert r.status_code == 409, r.text
    _assert_not_claimed(lead_id, sender)


def test_designed_lead_with_too_few_shots_is_blocked(designed_env, tmp_path):
    client, sender = designed_env
    lead_id = _designed(tmp_path)
    lead = import_store.get_lead(lead_id)
    design = lead["design_json"]
    design["shot_fingerprints"] = design["shot_fingerprints"][:1]
    import_store.update_lead(lead_id, flaw_shots=lead["flaw_shots"][:1], design_json=design,
                             html_body=lead["html_body"].replace("cid:flaw2", ""))
    r = _post(client, lead_id)
    assert r.status_code == 409, r.text
    _assert_not_claimed(lead_id, sender)


def test_stale_audit_blocks_the_send_with_a_re_audit_message(designed_env, tmp_path):
    client, sender = designed_env
    lead_id = _designed(tmp_path)
    import_store.update_lead(lead_id, audited_at=_utc_days_ago(10))
    r = _post(client, lead_id)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, dict) and detail["message"] and detail["warnings"]
    assert "re-audit" in (detail["message"] + " ".join(detail["warnings"])).lower()
    assert "resend_with" not in detail
    _assert_not_claimed(lead_id, sender)


@pytest.mark.parametrize("audited_at", [None, "", "not a date"])
def test_missing_or_unparseable_audit_time_blocks_a_designed_send(designed_env, tmp_path, audited_at):
    client, sender = designed_env
    lead_id = _designed(tmp_path)
    import_store.update_lead(lead_id, audited_at=audited_at)
    r = _post(client, lead_id)
    assert r.status_code == 409 and "re-audit" in r.text.lower()
    _assert_not_claimed(lead_id, sender)


def test_stale_audit_cannot_be_overridden_by_acknowledge_warnings(designed_env, tmp_path):
    client, sender = designed_env
    lead_id = _designed(tmp_path)
    import_store.update_lead(lead_id, audited_at=_utc_days_ago(10))
    r = _post(client, lead_id, acknowledge_warnings=True)
    assert r.status_code == 409 and "re-audit" in r.text.lower()
    _assert_not_claimed(lead_id, sender)


def test_fresh_audit_is_allowed(designed_env, tmp_path, monkeypatch):
    client, sender = designed_env
    lead_id = _designed(tmp_path)
    import_store.update_lead(lead_id, audited_at=_utc_days_ago(6.9))
    assert _post(client, lead_id).status_code == 200
    # 0 disables the age check entirely.
    monkeypatch.setattr(app_module.config, "DRAFT_STALE_DAYS", 0)
    old = _designed(tmp_path, sub="old", email="old@oldco.com", domain="oldco.com")
    import_store.update_lead(old, audited_at=_utc_days_ago(400))
    assert _post(client, old, email="old@oldco.com").status_code == 200
    assert len(sender.designed_calls) == 2


def test_designed_send_failure_marks_lead_failed_with_the_error(designed_env, tmp_path, monkeypatch):
    client, _ = designed_env
    monkeypatch.setattr(app_module, "ses", _Sender(error=Exception("SES rejected: bad domain")))
    lead_id = _designed(tmp_path)
    r = _post(client, lead_id)
    assert r.status_code == 500
    lead = import_store.get_lead(lead_id)
    assert lead["status"] == "failed" and "SES rejected" in lead["error"]


def test_a_suppressed_designed_recipient_is_marked_skipped(designed_env, tmp_path):
    client, sender = designed_env
    sender.result = False
    lead_id = _designed(tmp_path)
    assert _post(client, lead_id).status_code == 400
    lead = import_store.get_lead(lead_id)
    assert lead["status"] == "skipped" and lead["skip_reason"] == "suppressed"


def test_a_designed_double_post_sends_exactly_once(designed_env, tmp_path):
    client, sender = designed_env
    lead_id = _designed(tmp_path)
    assert _post(client, lead_id).status_code == 200
    assert _post(client, lead_id).status_code == 409
    assert len(sender.designed_calls) == 1 and sender.calls == []


def test_designed_send_logs_history_with_designed_variant_and_links_history_id(designed_env, tmp_path):
    client, _ = designed_env
    lead_id = _designed(tmp_path)
    stored = import_store.get_lead(lead_id)
    assert _post(client, lead_id).status_code == 200
    lead = import_store.get_lead(lead_id)
    assert lead["status"] == "sent" and lead["sent_at"] and lead["history_id"]
    row = db.get_email_history_by_id(lead["history_id"])
    assert row["variant"] == "import-designed" == send_policy.IMPORT_DESIGNED_VARIANT
    assert row["subject"] == stored["subject"] and row["body"] == stored["body"]
    assert row["website"] == "acme.com" and row["company"] == "Acme Bank"


def test_designed_send_passes_exactly_the_prepared_bytes(designed_env, tmp_path, monkeypatch):
    client, sender = designed_env
    lead_id = _designed(tmp_path)
    fingerprints = import_store.get_lead(lead_id)["design_json"]["shot_fingerprints"]
    real = email_images.prepare_all
    prepared_lists = []

    def spy(shots):
        out = real(shots)
        prepared_lists.append(out)
        return out

    monkeypatch.setattr(email_images, "prepare_all", spy)
    assert _post(client, lead_id).status_code == 200
    assert len(prepared_lists) == 1   # prepared once, never again at send time
    sent = sender.designed_calls[0]["images"]
    assert sent is prepared_lists[0]
    assert [hashlib.sha256(data).hexdigest() for _, data in sent] == [f["sha256"] for f in fingerprints]


def test_bounce_pause_counts_designed_and_text_sends_together(designed_env, monkeypatch):
    rows = [
        {"variant": "import-automation", "sent": 10, "bounced": 1},
        {"variant": "import-designed", "sent": 10, "bounced": 1},
        {"variant": "short", "sent": 100, "bounced": 100},
    ]
    status = send_policy.bounce_status(rows, 20, 5)
    assert status["sent"] == 20 and status["bounced"] == 2 and status["paused"] is True
    assert set(send_policy.IMPORT_VARIANTS) == {"import-automation", "import-designed"}
    # Designed bounces alone also pause text sends.
    client, sender = designed_env
    monkeypatch.setattr(
        app_module.db, "get_variant_performance",
        lambda: [{"variant": "import-designed", "sent": 20, "bounced": 2}],
    )
    r = _post(client, _approved())
    assert r.status_code == 409 and r.json()["detail"]["code"] == "import_paused"
    assert sender.calls == []


def test_all_existing_import_gates_still_apply_to_designed_leads(designed_env, tmp_path, monkeypatch):
    client, sender = designed_env

    # US recipient without a postal address.
    lead_id = _designed(tmp_path, sub="us")
    monkeypatch.setattr(app_module.config, "MAILING_ADDRESS", "")
    r = _post(client, lead_id)
    assert r.status_code == 409 and "MAILING_ADDRESS" in r.text
    _assert_not_claimed(lead_id, sender)
    monkeypatch.setattr(app_module.config, "MAILING_ADDRESS", "1 Example Road, Mumbai")

    # Bounce pause.
    monkeypatch.setattr(
        app_module.db, "get_variant_performance",
        lambda: [{"variant": "import-automation", "sent": 20, "bounced": 2}],
    )
    r = _post(client, lead_id)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "import_paused"
    _assert_not_claimed(lead_id, sender)
    monkeypatch.setattr(app_module.db, "get_variant_performance", lambda: [])

    # Daily cap.
    monkeypatch.setattr(app_module.db, "count_emails_sent_today", lambda: 50)
    assert _post(client, lead_id).status_code == 429
    _assert_not_claimed(lead_id, sender)
    monkeypatch.setattr(app_module.db, "count_emails_sent_today", lambda: 0)

    # The address must match the lead.
    assert _post(client, lead_id, email="someone@else.com").status_code == 409
    _assert_not_claimed(lead_id, sender)

    # Warnings need acknowledgement.
    warned = _designed(tmp_path, sub="warned", email="warned@acme.com", review_warnings=["w"])
    r = _post(client, warned, email="warned@acme.com")
    assert r.status_code == 409 and "acknowledge_warnings" in r.text
    _assert_not_claimed(warned, sender)
    assert _post(client, warned, email="warned@acme.com", acknowledge_warnings=True).status_code == 200
    assert len(sender.designed_calls) == 1

    # Suppression after approval is caught at send time.
    db.add_suppression("jane@acme.com", "unsub")
    assert _post(client, lead_id).status_code == 409
    lead = import_store.get_lead(lead_id)
    assert lead["status"] == "skipped" and lead["skip_reason"] == "suppressed"
    assert len(sender.designed_calls) == 1



def test_a_box_that_is_not_visible_blocks_with_its_own_message(designed_env, tmp_path, monkeypatch):
    client, sender = designed_env
    lead_id = _designed(tmp_path)

    def invisible(shots):
        raise email_images.BoxNotVisibleError("box too small")
    monkeypatch.setattr(app_module.import_email_images, "prepare_all", invisible)
    r = _post(client, lead_id)
    assert r.status_code == 409 and "box" in r.text and "missing on disk" not in r.text
    _assert_not_claimed(lead_id, sender)
