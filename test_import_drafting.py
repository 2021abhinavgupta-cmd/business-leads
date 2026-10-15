import json
import time

import pytest

import app as app_module
from analyzer.ai_audit import AIAuditor
from importer import compose, drafting
from storage import import_store
from import_test_support import client_with_key, make_row, seed_lead, use_temp_db

LEAD = make_row()
GOOD = {
    "subject": "Vendor onboarding",
    "paragraphs": ["Acme Bank serves families across Boston. Is vendor onboarding still handled by hand?"],
    "source_quote": "serves families across Boston",
}
HOMEPAGE = "Acme Bank serves families across Boston and offers wealth management."


def _auditor():
    return AIAuditor.__new__(AIAuditor)


def test_prompt_contains_no_dashes_and_carries_the_guards():
    prompt = _auditor()._build_import_prompt(LEAD, HOMEPAGE, "Kshitij Gupta")
    assert not any(d in prompt for d in ("-", "—", "–"))
    assert "Acme Bank serves families" in prompt
    assert "hypothetical" in prompt and "50 characters" in prompt and "60 words" in prompt
    assert "question instead of a statement" in prompt


def test_prompt_marks_missing_homepage_text():
    prompt = _auditor()._build_import_prompt(LEAD, None, "Kshitij Gupta")
    assert "(not available)" in prompt


def test_parse_import_json_accepts_fences_and_cleans():
    raw = "```json\n" + json.dumps({"subject": " S ", "paragraphs": ["a", " ", "b"], "source_quote": " q "}) + "\n```"
    assert AIAuditor._parse_import_json(raw) == {"subject": "S", "paragraphs": ["a", "b"], "source_quote": "q"}


@pytest.mark.parametrize("raw", [
    "not json", "{}", '{"subject": "S", "paragraphs": []}', '{"subject": "", "paragraphs": ["a"]}',
    '{"subject": "S", "paragraphs": "a"}',
])
def test_parse_import_json_rejects_bad_shapes(raw):
    assert AIAuditor._parse_import_json(raw) is None


def test_generate_import_email_falls_through_providers(monkeypatch):
    auditor = _auditor()
    monkeypatch.setattr(auditor, "_call_import_anthropic", lambda p: None)
    monkeypatch.setattr(auditor, "_call_import_gemini", lambda p: ("garbage", 0.1))
    monkeypatch.setattr(auditor, "_call_import_openai", lambda p: (json.dumps(GOOD), 0.0004))
    result = auditor.generate_import_email(LEAD, HOMEPAGE, "Kshitij Gupta")
    assert result["subject"] == "Vendor onboarding" and result["ai_cost"] == 0.0004


def test_generate_import_email_returns_none_when_all_fail(monkeypatch):
    auditor = _auditor()
    for name in ("_call_import_anthropic", "_call_import_gemini", "_call_import_openai"):
        monkeypatch.setattr(auditor, name, lambda p: None)
    assert auditor.generate_import_email(LEAD, HOMEPAGE, "K") is None


class _FakeAuditor:
    def __init__(self, result):
        self.result = result

    def generate_import_email(self, lead, homepage_text, your_name):
        return self.result


def test_draft_lead_assembles_body_and_checks():
    out = drafting.draft_lead(
        LEAD, _FakeAuditor({**GOOD, "ai_cost": 0.001}), your_name="Kshitij Gupta",
        fetch=lambda domain: HOMEPAGE,
    )
    assert out["subject"] == "Vendor onboarding"
    assert out["body"].startswith("Hi Jane,") and compose.FIXED_LINE in out["body"]
    assert out["review_warnings"] == []
    assert out["source_quote"] == GOOD["source_quote"] and out["ai_cost"] == 0.001


def test_draft_lead_without_homepage_is_flagged():
    out = drafting.draft_lead(
        LEAD, _FakeAuditor({**GOOD, "source_quote": "", "ai_cost": 0.0}), your_name="K",
        fetch=lambda domain: None,
    )
    assert any(w.startswith("No homepage evidence") for w in out["review_warnings"])


def test_draft_lead_flags_overlong_subject_and_body():
    long_para = " ".join(["word"] * 70)
    out = drafting.draft_lead(
        LEAD, _FakeAuditor({"subject": "s" * 60, "paragraphs": [long_para], "source_quote": "", "ai_cost": 0}),
        your_name="K", fetch=lambda d: None,
    )
    joined = " ".join(out["review_warnings"])
    assert "Subject is 60 characters" in joined and "Body is 70 words" in joined


def test_draft_lead_raises_when_every_provider_failed():
    with pytest.raises(drafting.DraftError):
        drafting.draft_lead(LEAD, _FakeAuditor(None), your_name="K", fetch=lambda d: None)


# ---- routes --------------------------------------------------------------

def _fake_draft(lead, auditor, *, your_name):
    return {
        "subject": "Vendor onboarding", "body": "Hi Jane,\n\nBody", "review_warnings": ["w"],
        "source_quote": "q", "ai_cost": 0.002,
    }


def test_draft_route_saves_the_draft_and_logs_cost(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    monkeypatch.setattr(app_module.import_drafting, "draft_lead", _fake_draft)
    lead_id = seed_lead()
    r = client.post(f"/api/import/leads/{lead_id}/draft")
    assert r.status_code == 200, r.text
    lead = r.json()["lead"]
    assert lead["status"] == "drafted" and lead["review_warnings"] == ["w"] and lead["source_quote"] == "q"
    assert any(c["category"] == "AI Import Draft" for c in app_module.db.get_costs())


def test_draft_route_rejects_a_sent_lead_and_unknown_lead(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    assert client.post("/api/import/leads/999/draft").status_code == 404
    lead_id = seed_lead(status="sent")
    assert client.post(f"/api/import/leads/{lead_id}/draft").status_code == 409


def test_draft_route_records_an_ai_failure_and_keeps_the_lead_ready(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)

    def fail(lead, auditor, *, your_name):
        raise drafting.DraftError("Every AI provider failed")

    monkeypatch.setattr(app_module.import_drafting, "draft_lead", fail)
    lead_id = seed_lead()
    r = client.post(f"/api/import/leads/{lead_id}/draft")
    assert r.status_code == 502
    lead = import_store.get_lead(lead_id)
    assert lead["status"] == "ready" and "Every AI provider failed" in lead["error"]


def test_draft_route_times_out_cleanly(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    monkeypatch.setattr(app_module, "IMPORT_DRAFT_TIMEOUT_SECONDS", 0.05)

    def slow(lead, auditor, *, your_name):
        time.sleep(0.4)
        return _fake_draft(lead, auditor, your_name=your_name)

    monkeypatch.setattr(app_module.import_drafting, "draft_lead", slow)
    lead_id = seed_lead()
    r = client.post(f"/api/import/leads/{lead_id}/draft")
    assert r.status_code == 504
    assert import_store.get_lead(lead_id)["status"] == "ready"


def test_edit_route_rechecks_and_returns_the_lead_to_drafted(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    lead_id = seed_lead(
        status="approved", subject="S", body="Hi Jane,\n\nOld", review_warnings=["No homepage evidence: x"],
    )
    r = client.put(f"/api/import/leads/{lead_id}", json={"subject": "New subject", "body": "Hi Jane,\n\nA co-op"})
    assert r.status_code == 200, r.text
    lead = r.json()["lead"]
    assert lead["status"] == "drafted" and lead["subject"] == "New subject"
    assert any("dash" in w for w in lead["review_warnings"])
    assert "No homepage evidence: x" in lead["review_warnings"]


def test_edit_route_rejects_empty_and_unedited_states(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    fresh = seed_lead()
    assert client.put(f"/api/import/leads/{fresh}", json={"subject": "S", "body": "B"}).status_code == 409
    drafted = seed_lead(status="drafted", subject="S", body="B")
    assert client.put(f"/api/import/leads/{drafted}", json={"subject": " ", "body": "B"}).status_code == 400
