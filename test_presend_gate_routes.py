"""The pre-send gate applies to every send route, not only Import: Dashboard
emails (agriculture and textile included), manual follow-ups, social emails."""
import pytest
from fastapi.testclient import TestClient

import app as app_module


class _Sender:
    def __init__(self):
        self.calls = 0

    def send_email(self, *a, **kw):
        self.calls += 1
        return "<id@x>"

    def send_followup(self, *a, **kw):
        self.calls += 1
        return True


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module.config, "API_KEY", None)
    monkeypatch.setattr(app_module.db, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(app_module.db, "DB_PATH", str(tmp_path / "t.sqlite"))
    sender = _Sender()
    monkeypatch.setattr(app_module, "get_sender", lambda: sender)
    monkeypatch.setattr(app_module, "ses", sender)
    import uuid
    c = TestClient(app_module.app, raise_server_exceptions=False,
                   headers={"X-API-Key": uuid.uuid4().hex})   # own rate-limit bucket
    c.sender = sender
    return c


BAD_BODIES = ["Hi {{first_name}}, your site is slow.", "I hope this email finds you well. Your site is slow."]


@pytest.mark.parametrize("body", BAD_BODIES)
def test_dashboard_send_is_blocked_even_when_acknowledged(client, body):
    r = client.post("/api/send", json={
        "email": "owner@acme.com", "subject": "Your site", "body": body,
        "company": "Acme", "website": "https://acme.com", "acknowledge_warnings": True,
    })
    assert r.status_code == 409 and r.json()["detail"]["code"] == "presend_gate"
    assert client.sender.calls == 0


@pytest.mark.parametrize("body", BAD_BODIES)
def test_social_email_is_blocked(client, body):
    r = client.post("/api/social/send", json={
        "company": "Acme", "target": "owner@acme.com", "subject": "Your IG", "body": body,
    })
    assert r.status_code == 409 and r.json()["detail"]["code"] == "presend_gate"
    assert client.sender.calls == 0


def test_followup_is_blocked(client, monkeypatch):
    monkeypatch.setattr(app_module.db, "count_emails_sent_today", lambda: 0)
    monkeypatch.setattr(
        app_module.db, "get_email_history_by_id",
        lambda i: {"target_email": "owner@acme.com", "message_id": "<m@x>"},
    )
    r = client.post("/api/send-followup", json={
        "history_id": 1, "subject": "Re: Your site", "body": "Hi {{name}}, any thoughts?",
    })
    assert r.status_code == 409 and r.json()["detail"]["code"] == "presend_gate"
    assert client.sender.calls == 0


def test_a_clean_social_email_still_sends(client):
    r = client.post("/api/social/send", json={
        "company": "Acme", "target": "owner@acme.com", "subject": "Your IG went quiet",
        "body": "Hi Sam, your last post was in March.",
    })
    assert r.status_code == 200 and client.sender.calls == 1
