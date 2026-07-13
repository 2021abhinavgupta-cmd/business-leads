"""
Unit tests for the spam-deliverability fixes in emailer/ses_sender.py and
storage/db.py — no network, no AWS credentials required. Locks in: the
suppression list actually blocking sends, and the List-Unsubscribe header
shape (mailto-only vs one-click) driven by APP_BASE_URL.
"""

import config
from storage import db
from emailer.ses_sender import SESSender


def _use_temp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.sqlite"))


def test_suppression_roundtrip(monkeypatch, tmp_path):
    _use_temp_db(monkeypatch, tmp_path)
    assert db.is_suppressed("lead@example.com") is False
    db.add_suppression("Lead@Example.com")
    assert db.is_suppressed("lead@example.com") is True
    assert db.is_suppressed("someone-else@example.com") is False


def test_log_email_stores_message_id(monkeypatch, tmp_path):
    _use_temp_db(monkeypatch, tmp_path)
    db.log_email("Acme", "acme.com", "lead@acme.com", "us@x.com", "Subject", "Body", message_id="<abc123@x.com>")
    history = db.get_email_history()
    assert history[0]["message_id"] == "<abc123@x.com>"


def test_unsubscribe_headers_mailto_only(monkeypatch):
    monkeypatch.setattr(config, "APP_BASE_URL", "")
    monkeypatch.setattr(config, "FROM_EMAIL", "outreach@mmga.agency")
    ses = SESSender()
    headers = ses._unsubscribe_headers("lead@example.com")
    assert "mailto:outreach@mmga.agency?subject=Unsubscribe" in headers["List-Unsubscribe"]
    assert "List-Unsubscribe-Post" not in headers


def test_unsubscribe_headers_one_click(monkeypatch):
    monkeypatch.setattr(config, "APP_BASE_URL", "https://myapp.up.railway.app")
    monkeypatch.setattr(config, "FROM_EMAIL", "outreach@mmga.agency")
    ses = SESSender()
    headers = ses._unsubscribe_headers("lead@example.com")
    assert "https://myapp.up.railway.app/unsubscribe?email=lead%40example.com" in headers["List-Unsubscribe"]
    assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_send_email_skips_suppressed_recipient(monkeypatch, tmp_path):
    _use_temp_db(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "FROM_EMAIL", "outreach@mmga.agency")
    db.add_suppression("lead@example.com")

    ses = SESSender()
    result = ses.send_email("lead@example.com", "Subject", "Body")
    assert result is False


def test_send_followup_skips_suppressed_recipient(monkeypatch, tmp_path):
    _use_temp_db(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "FROM_EMAIL", "outreach@mmga.agency")
    db.add_suppression("lead@example.com")

    ses = SESSender()
    result = ses.send_followup("lead@example.com", "Subject", "Body")
    assert result is False
