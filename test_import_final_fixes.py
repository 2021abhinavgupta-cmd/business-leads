"""Regression tests for the final whole-branch review fix wave (I1 and cheap fixes)."""
import types

import pytest

import app as app_module
import config
from analyzer import ai_audit
from analyzer.ai_audit import AIAuditor
from emailer.base_sender import BaseSender
from importer import csv_rules, drafting
from storage import db, import_store
from import_test_support import (
    client_with_key, csv_text, empty_blockers, make_row, seed_lead, use_temp_db,
)
from test_import_send import _Sender, _approved, _post
from fastapi.testclient import TestClient


# ---- I1: /unsubscribe -----------------------------------------------------

@pytest.fixture
def anon(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    monkeypatch.setattr(app_module.sheets, "find_row_by_email", lambda e: None, raising=False)
    return TestClient(app_module.app, raise_server_exceptions=False)


def test_get_unsubscribe_does_not_suppress_and_shows_a_form(anon):
    r = anon.get("/unsubscribe", params={"email": "a<b>@x.com"})
    assert r.status_code == 200
    assert not db.is_suppressed("a<b>@x.com")
    assert "<form method='post'" in r.text and "Confirm unsubscribe" in r.text
    assert "a&lt;b&gt;@x.com" in r.text and "a<b>@x.com" not in r.text
    assert "/unsubscribe?email=a%3Cb%3E%40x.com" in r.text


def test_post_unsubscribe_suppresses_and_one_click_gets_json(anon):
    r = anon.post("/unsubscribe", params={"email": "a@x.com"})
    assert r.status_code == 200 and r.json() == {"status": "unsubscribed"}
    assert db.is_suppressed("a@x.com")


def test_browser_form_post_gets_the_confirmation_page(anon):
    r = anon.post("/unsubscribe", params={"email": "b@x.com"}, headers={"Accept": "text/html,*/*"})
    assert "You've been unsubscribed" in r.text
    assert db.is_suppressed("b@x.com")


@pytest.mark.parametrize("method", ["get", "post"])
def test_unsubscribe_without_email_is_400(anon, method):
    assert getattr(anon, method)("/unsubscribe").status_code == 400


# ---- csv_rules ------------------------------------------------------------

@pytest.mark.parametrize("email,domain", [
    ("a@mca.gov.in", "x.com"), ("a@x.com", "mca.gov.in"),
    ("a@dept.nic.in", "x.com"), ("a@x.com", "dept.nic.in"),
])
def test_indian_government_domains_are_skipped(email, domain):
    out = csv_rules.apply_rules([make_row(email=email, domain=domain)], empty_blockers())
    assert out[0]["skip_reason"] == "gov_domain"


@pytest.mark.parametrize("raw", ["USA", "US", "U.S.", "u.s.a.", " United States of America ", "united states"])
def test_country_aliases_normalise_to_united_states(raw):
    text = csv_text([make_row(country=raw)])
    assert csv_rules.parse_csv(text)[0]["country"] == "united states"


def test_india_is_kept():
    assert csv_rules.parse_csv(csv_text([make_row(country="India")]))[0]["country"] == "india"


@pytest.mark.parametrize("email", [
    "a b@x.com", "a,b@x.com", "a;b@x.com", "<a@x.com>", 'a"b@x.com', "(a)@x.com",
    "a[b]@x.com", "a\\b@x.com", "a@x.com,b@y.com", "a@x .com",
])
def test_email_regex_rejects_bad_characters(email):
    assert csv_rules._valid_email(email) is False


def test_email_regex_still_accepts_normal_addresses():
    assert csv_rules._valid_email("first.last+tag@sub.example.co.in")


def test_seniority_is_normalised():
    assert csv_rules.parse_csv(csv_text([make_row(person_seniority="C-Suite")]))[0]["person_seniority"] == "c_suite"
    assert csv_rules.parse_csv(csv_text([make_row(person_seniority="Senior Manager")]))[0]["person_seniority"] == "senior_manager"


def test_trailing_dot_is_stripped_from_domains():
    assert csv_rules.parse_csv(csv_text([make_row(domain="https://www.Acme.com./x")]))[0]["domain"] == "acme.com"


# ---- subject newlines -----------------------------------------------------

class _Auditor:
    def generate_import_email(self, lead, homepage, name):
        return {"subject": "Line one\r\nBcc: evil@x.com", "paragraphs": ["Hello there."],
                "source_quote": "", "ai_cost": 0.0}


def test_draft_lead_strips_crlf_from_the_subject():
    out = drafting.draft_lead(make_row(), _Auditor(), your_name="K", fetch=lambda d: None)
    assert "\r" not in out["subject"] and "\n" not in out["subject"]
    assert out["subject"] == "Line one Bcc: evil@x.com"


def test_save_edit_strips_crlf_from_the_subject(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    lead_id = seed_lead(status="drafted", subject="S", body="Hi Jane,\n\nok")
    r = client.put(f"/api/import/leads/{lead_id}", json={"subject": " A\r\nB ", "body": "Hi Jane,\n\nok"})
    assert r.status_code == 200
    assert import_store.get_lead(lead_id)["subject"] == "A B"


# ---- send path: cost log failure ------------------------------------------

def test_a_log_cost_failure_still_logs_history_and_links_the_lead(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    monkeypatch.setattr(app_module.config, "MAILING_ADDRESS", "1 Example Road, Mumbai")
    monkeypatch.setattr(app_module.config, "DAILY_EMAIL_LIMIT", 50)
    sender = _Sender()
    monkeypatch.setattr(app_module, "ses", sender)
    client = client_with_key(monkeypatch)
    lead_id = _approved()

    def boom(*a, **k):
        raise RuntimeError("cost table locked")

    monkeypatch.setattr(app_module.db, "log_cost", boom)
    assert _post(client, lead_id).status_code == 200
    lead = import_store.get_lead(lead_id)
    assert lead["status"] == "sent" and lead["history_id"]


# ---- provider timeouts ----------------------------------------------------

def _auditor():
    return AIAuditor.__new__(AIAuditor)


def test_import_anthropic_call_passes_a_timeout():
    seen = {}

    def create(**kw):
        seen.update(kw)
        return types.SimpleNamespace(
            usage=types.SimpleNamespace(input_tokens=1, output_tokens=1),
            content=[types.SimpleNamespace(type="tool_use", input={"subject": "s"})],
        )

    a = _auditor()
    a._anthropic_client = types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
    assert a._call_import_anthropic("p") is not None
    assert seen["timeout"] == 30


def test_import_openai_call_passes_a_timeout():
    seen = {}

    def create(**kw):
        seen.update(kw)
        return types.SimpleNamespace(
            usage=types.SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="{}"))],
        )

    a = _auditor()
    a._openai_client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
    assert a._call_import_openai("p") is not None
    assert seen["timeout"] == 30


def test_import_gemini_call_passes_a_timeout(monkeypatch):
    seen = {}

    class Model:
        def __init__(self, *a, **k):
            pass

        def generate_content(self, prompt, **kw):
            seen.update(kw)
            return types.SimpleNamespace(text="{}", usage_metadata=None)

    monkeypatch.setattr(ai_audit.config, "GEMINI_API_KEY", "k")
    monkeypatch.setattr(ai_audit.genai, "GenerativeModel", Model)
    monkeypatch.setattr(ai_audit.genai.types, "GenerationConfig", lambda **k: None)
    assert _auditor()._call_import_gemini("p") is not None
    assert seen["request_options"] == {"timeout": 30}


# ---- HTML escaping --------------------------------------------------------

def test_html_part_escapes_body_and_footer_but_plain_part_is_untouched(monkeypatch):
    monkeypatch.setattr(config, "MAILING_ADDRESS", "A & B <Road>, Mumbai")
    monkeypatch.setattr(config, "APP_BASE_URL", "")
    msg = BaseSender(from_email="us@example.com")._build_initial_message(
        "a@b.com", "S", "Use <b>this</b> & that\nline2", "<m@x>")
    plain = html = ""
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            plain = part.get_payload(decode=True).decode("utf-8")
        elif part.get_content_type() == "text/html":
            html = part.get_payload(decode=True).decode("utf-8")
    assert "Use <b>this</b> & that\nline2" in plain and "A & B <Road>" in plain
    assert "Use &lt;b&gt;this&lt;/b&gt; &amp; that<br>line2" in html
    assert "A &amp; B &lt;Road&gt;" in html


# ---- upload size ----------------------------------------------------------

def test_oversized_csv_text_is_rejected_early_with_422(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    r = client.post("/api/import/upload", json={"csv_text": "a" * 5_300_001})
    assert r.status_code == 422
