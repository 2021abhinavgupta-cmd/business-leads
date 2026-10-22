"""The deterministic pre-send gate for imported leads (idea from Harvey, MIT)."""
import pytest

from importer import presend_gate, send_policy
from importer.presend_gate import presend_problems

OK = ("a@acme.com", "Quick question about Acme", "Hi Sam,\n\nYour contact page hides the phone number.")


def test_a_clean_email_passes():
    assert presend_problems(*OK) == []


@pytest.mark.parametrize("to", ["", "not-an-email", "a@b", "a b@acme.com"])
def test_a_bad_recipient_is_blocked(to):
    assert any("recipient" in p for p in presend_problems(to, OK[1], OK[2]))


def test_empty_subject_and_body_are_blocked():
    out = presend_problems(OK[0], "  ", "")
    assert "empty subject" in out and "empty body" in out


def test_a_long_subject_is_blocked():
    assert any("subject too long" in p for p in presend_problems(OK[0], "x" * 91, OK[2]))


@pytest.mark.parametrize("text", ["Hi {{first_name},", "Hi {{ name }},", "Hi {name},", "Hi [First Name],"])
def test_unfilled_placeholders_are_blocked(text):
    assert any("placeholder" in p for p in presend_problems(OK[0], OK[1], text))


@pytest.mark.parametrize("text", ["Click here now", "I hope this email finds you well", "a real game-changer"])
def test_spam_and_ai_tell_phrases_are_blocked(text):
    assert any("phrase" in p for p in presend_problems(OK[0], OK[1], text))


def test_phrases_match_whole_words_only():
    # "winner" is not on the list and "act now" must not match inside "exact nowhere".
    assert presend_problems(OK[0], OK[1], "Your winners page is exact nowhere near the top.") == []


def _lead(**over):
    lead = {
        "id": 1, "status": "approved", "email": "a@acme.com", "country": "India",
        "subject": OK[1], "body": OK[2], "review_warnings": [],
    }
    lead.update(over)
    return lead


def _check(lead, **kw):
    send_policy.check_import_send(
        lead, req_email="a@acme.com", acknowledge_warnings=kw.get("ack", False),
        mailing_address="", blockers={"emails": set(), "domains": set()},
        bounce={"paused": False},
    )


def test_gate_runs_inside_the_send_policy_and_cannot_be_acknowledged(monkeypatch):
    monkeypatch.setattr(send_policy.csv_rules, "recheck", lambda lead, blockers: None)
    with pytest.raises(send_policy.SendBlocked) as e:
        _check(_lead(body="Hi {{name}},"), ack=True)
    assert e.value.status_code == 409 and e.value.detail["code"] == "presend_gate"


def test_a_clean_lead_still_sends(monkeypatch):
    monkeypatch.setattr(send_policy.csv_rules, "recheck", lambda lead, blockers: None)
    _check(_lead())
