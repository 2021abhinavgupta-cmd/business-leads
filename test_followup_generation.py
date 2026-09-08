"""
Tests for the History tab's "Generate Follow-up" button (added 2026-09-08).

Unlike scheduler.py's automated 3-email sequence — which uses
BaseSender.generate_followup's hardcoded, name-and-stage-only copy because
it has no access to the original audit — this reads one specific past send
(by its email_history row id) and drafts a follow-up that references what
that exact email actually said, via AIAuditor.generate_followup_from_original.

Unit tests only — every AI provider call and DB write is stubbed.
"""

import config
from storage import db
from analyzer.ai_audit import AIAuditor


def _use_temp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.sqlite"))


# ---------------------------------------------------------------------------
# storage/db.py: get_email_history_by_id
# ---------------------------------------------------------------------------

def test_get_email_history_by_id_returns_none_when_missing(monkeypatch, tmp_path):
    _use_temp_db(monkeypatch, tmp_path)
    assert db.get_email_history_by_id(999) is None


def test_get_email_history_by_id_returns_the_exact_row(monkeypatch, tmp_path):
    _use_temp_db(monkeypatch, tmp_path)
    db.log_email("Acme", "acme.com", "lead@acme.com", "us@x.com", "Subject A", "Body A", message_id="<a@x.com>")
    db.log_email("Beta", "beta.com", "lead@beta.com", "us@x.com", "Subject B", "Body B", message_id="<b@x.com>")

    history = db.get_email_history()
    beta_id = next(row["id"] for row in history if row["company"] == "Beta")

    row = db.get_email_history_by_id(beta_id)
    assert row["company"] == "Beta"
    assert row["subject"] == "Subject B"
    assert row["target_email"] == "lead@beta.com"
    assert row["message_id"] == "<b@x.com>"


# ---------------------------------------------------------------------------
# AIAuditor._parse_followup_json
# ---------------------------------------------------------------------------

def test_parse_followup_json_accepts_plain_json():
    result = AIAuditor._parse_followup_json('{"subject": "Re: Hi", "body": "Just checking in."}')
    assert result == {"subject": "Re: Hi", "body": "Just checking in."}


def test_parse_followup_json_strips_markdown_fences():
    raw = '```json\n{"subject": "Re: Hi", "body": "Following up."}\n```'
    result = AIAuditor._parse_followup_json(raw)
    assert result == {"subject": "Re: Hi", "body": "Following up."}


def test_parse_followup_json_rejects_the_audit_schema():
    """A response shaped like the AUDIT schema (flaws/overall_score/...)
    must not be mistaken for a valid follow-up — the two schemas are
    deliberately kept separate (see _FOLLOWUP_RESPONSE_SCHEMA's docstring)."""
    raw = '{"flaws": [], "overall_score": 50, "email_subject": "x", "opening_line": "y"}'
    assert AIAuditor._parse_followup_json(raw) is None


def test_parse_followup_json_rejects_malformed_json():
    assert AIAuditor._parse_followup_json("not json at all") is None


# ---------------------------------------------------------------------------
# AIAuditor.generate_followup_from_original — provider fallback
# ---------------------------------------------------------------------------

def _auditor_with_stubbed_providers():
    auditor = AIAuditor.__new__(AIAuditor)
    return auditor


def test_generate_followup_uses_the_first_successful_provider(monkeypatch):
    auditor = _auditor_with_stubbed_providers()
    monkeypatch.setattr(auditor, "_call_followup_anthropic", lambda p: ('{"subject": "Re: Hi", "body": "Following up on my earlier note."}', 0.0003))
    monkeypatch.setattr(auditor, "_call_followup_gemini", lambda p: (_ for _ in ()).throw(AssertionError("should not reach Gemini")))
    monkeypatch.setattr(auditor, "_call_followup_openai", lambda p: (_ for _ in ()).throw(AssertionError("should not reach OpenAI")))

    result = auditor.generate_followup_from_original("Acme", "there", "Kshitij", "Original subject", "Original body", stage=1)
    assert result["subject"] == "Re: Hi"
    assert result["body"] == "Following up on my earlier note."
    assert result["ai_cost"] == 0.0003


def test_generate_followup_falls_through_to_the_next_provider(monkeypatch):
    auditor = _auditor_with_stubbed_providers()
    monkeypatch.setattr(auditor, "_call_followup_anthropic", lambda p: None)
    monkeypatch.setattr(auditor, "_call_followup_gemini", lambda p: ('{"subject": "Re: Hi", "body": "From Gemini."}', 0.0001))
    monkeypatch.setattr(auditor, "_call_followup_openai", lambda p: (_ for _ in ()).throw(AssertionError("should not reach OpenAI")))

    result = auditor.generate_followup_from_original("Acme", "there", "Kshitij", "Original subject", "Original body", stage=2)
    assert result["body"] == "From Gemini."


def test_generate_followup_returns_none_when_every_provider_fails(monkeypatch):
    auditor = _auditor_with_stubbed_providers()
    monkeypatch.setattr(auditor, "_call_followup_anthropic", lambda p: None)
    monkeypatch.setattr(auditor, "_call_followup_gemini", lambda p: None)
    monkeypatch.setattr(auditor, "_call_followup_openai", lambda p: None)

    assert auditor.generate_followup_from_original("Acme", "there", "Kshitij", "Original subject", "Original body") is None


def test_generate_followup_returns_none_on_an_unparseable_response(monkeypatch):
    auditor = _auditor_with_stubbed_providers()
    monkeypatch.setattr(auditor, "_call_followup_anthropic", lambda p: ("not valid json", 0.0001))
    monkeypatch.setattr(auditor, "_call_followup_gemini", lambda p: None)
    monkeypatch.setattr(auditor, "_call_followup_openai", lambda p: None)

    assert auditor.generate_followup_from_original("Acme", "there", "Kshitij", "Original subject", "Original body") is None


def test_generate_followup_prompt_forbids_new_claims_and_dashes():
    """
    The prompt itself is what keeps this from becoming a second, ungrounded
    audit — check the actual instructions sent to the model, not just that
    a response comes back.
    """
    auditor = _auditor_with_stubbed_providers()
    captured = {}

    def _capture(p):
        captured["prompt"] = p
        return None

    auditor._call_followup_anthropic = _capture
    auditor._call_followup_gemini = lambda p: None
    auditor._call_followup_openai = lambda p: None

    auditor.generate_followup_from_original("Acme", "Priya Shah", "Kshitij", "Your site is slow", "It takes 6 seconds to load.", stage=1)

    prompt = captured["prompt"]
    assert "Your site is slow" in prompt
    assert "It takes 6 seconds to load." in prompt
    assert "Do NOT introduce any new claim" in prompt
    assert "NEVER use hyphens" in prompt


# ---------------------------------------------------------------------------
# app.py wiring: /api/generate-followup and /api/send-followup
# ---------------------------------------------------------------------------

def _test_client(monkeypatch):
    from fastapi.testclient import TestClient
    import app as app_module

    monkeypatch.setattr(app_module.config, "API_KEY", None)
    return app_module, TestClient(app_module.app, raise_server_exceptions=False)


def test_generate_followup_404s_for_an_unknown_history_id(monkeypatch):
    app_module, client = _test_client(monkeypatch)
    monkeypatch.setattr(app_module.db, "get_email_history_by_id", lambda history_id: None)

    res = client.post("/api/generate-followup", json={"history_id": 12345})
    assert res.status_code == 404


def test_generate_followup_502s_when_every_provider_fails(monkeypatch):
    app_module, client = _test_client(monkeypatch)
    monkeypatch.setattr(app_module.db, "get_email_history_by_id", lambda history_id: {
        "company": "Acme", "subject": "Original", "body": "Body", "target_email": "lead@acme.com", "website": "acme.com", "message_id": "<a@x.com>",
    })
    monkeypatch.setattr(app_module.auditor, "generate_followup_from_original", lambda *a, **k: None)

    res = client.post("/api/generate-followup", json={"history_id": 1})
    assert res.status_code == 502


def test_generate_followup_returns_the_draft_and_logs_cost(monkeypatch):
    app_module, client = _test_client(monkeypatch)
    monkeypatch.setattr(app_module.db, "get_email_history_by_id", lambda history_id: {
        "company": "Acme", "subject": "Original", "body": "Body", "target_email": "lead@acme.com", "website": "acme.com", "message_id": "<a@x.com>",
    })
    monkeypatch.setattr(app_module.auditor, "generate_followup_from_original", lambda *a, **k: {"subject": "Re: Original", "body": "Following up.", "ai_cost": 0.0004})

    logged = {}
    monkeypatch.setattr(app_module.db, "log_cost", lambda category, cost, description="": logged.update(category=category, cost=cost))

    res = client.post("/api/generate-followup", json={"history_id": 1, "stage": 1})
    assert res.status_code == 200
    data = res.json()
    assert data["subject"] == "Re: Original"
    assert data["body"] == "Following up."
    assert data["to_email"] == "lead@acme.com"
    assert logged["category"] == "AI Followup"
    assert logged["cost"] == 0.0004


def test_send_followup_404s_for_an_unknown_history_id(monkeypatch):
    app_module, client = _test_client(monkeypatch)
    monkeypatch.setattr(app_module.db, "count_emails_sent_today", lambda: 0)
    monkeypatch.setattr(app_module.db, "get_email_history_by_id", lambda history_id: None)

    res = client.post("/api/send-followup", json={"history_id": 1, "subject": "s", "body": "b"})
    assert res.status_code == 404


def test_send_followup_400s_when_the_original_has_no_recipient(monkeypatch):
    app_module, client = _test_client(monkeypatch)
    monkeypatch.setattr(app_module.db, "count_emails_sent_today", lambda: 0)
    monkeypatch.setattr(app_module.db, "get_email_history_by_id", lambda history_id: {
        "company": "Acme", "website": "acme.com", "target_email": "", "message_id": "",
    })

    res = client.post("/api/send-followup", json={"history_id": 1, "subject": "s", "body": "b"})
    assert res.status_code == 400


def test_send_followup_429s_over_the_daily_cap(monkeypatch):
    app_module, client = _test_client(monkeypatch)
    monkeypatch.setattr(app_module.config, "DAILY_EMAIL_LIMIT", 5)
    monkeypatch.setattr(app_module.db, "count_emails_sent_today", lambda: 5)

    res = client.post("/api/send-followup", json={"history_id": 1, "subject": "s", "body": "b"})
    assert res.status_code == 429


def test_send_followup_success_threads_against_the_original_and_logs_the_variant(monkeypatch):
    app_module, client = _test_client(monkeypatch)
    monkeypatch.setattr(app_module.db, "count_emails_sent_today", lambda: 0)
    monkeypatch.setattr(app_module.db, "get_email_history_by_id", lambda history_id: {
        "company": "Acme", "website": "acme.com", "target_email": "lead@acme.com", "message_id": "<original@x.com>",
    })

    sent = {}

    def _fake_send_followup(to_email, subject, body, in_reply_to=""):
        sent.update(to_email=to_email, subject=subject, body=body, in_reply_to=in_reply_to)
        return True

    monkeypatch.setattr(app_module.ses, "send_followup", _fake_send_followup)
    monkeypatch.setattr(app_module.db, "log_cost", lambda *a, **k: None)

    logged_email = {}
    monkeypatch.setattr(
        app_module.db, "log_email",
        lambda company, website, target_email, sender_email, subject, body, message_id="", variant="":
            logged_email.update(company=company, website=website, target_email=target_email, subject=subject, body=body, variant=variant),
    )

    res = client.post("/api/send-followup", json={"history_id": 1, "subject": "Re: Original", "body": "Following up."})
    assert res.status_code == 200
    assert sent["to_email"] == "lead@acme.com"
    assert sent["in_reply_to"] == "<original@x.com>"
    assert logged_email["variant"] == "followup-ai"
    assert logged_email["company"] == "Acme"


def test_send_followup_400s_when_the_transport_reports_failure(monkeypatch):
    app_module, client = _test_client(monkeypatch)
    monkeypatch.setattr(app_module.db, "count_emails_sent_today", lambda: 0)
    monkeypatch.setattr(app_module.db, "get_email_history_by_id", lambda history_id: {
        "company": "Acme", "website": "acme.com", "target_email": "lead@acme.com", "message_id": "<original@x.com>",
    })
    monkeypatch.setattr(app_module.ses, "send_followup", lambda *a, **k: False)

    res = client.post("/api/send-followup", json={"history_id": 1, "subject": "s", "body": "b"})
    assert res.status_code == 400
