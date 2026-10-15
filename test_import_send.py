import pytest

import app as app_module
from importer import send_policy
from storage import db, import_store
from import_test_support import client_with_key, empty_blockers, seed_lead, use_temp_db

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

    def send_email(self, to_email, subject, body, **kw):
        self.calls.append({"to": to_email, "subject": subject, "body": body, **kw})
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
