"""
Unit tests for AIAuditor._parse_json — no network, no API keys required.
Runs on every push; this is the one real regression guard in the suite.
"""

from analyzer.ai_audit import AIAuditor, _AI_TEMPERATURE


def test_parse_json_valid():
    raw = (
        '```json\n'
        '{"flaws": [], "overall_score": 50, '
        '"email_subject": "Hi", "opening_line": "Hey"}\n'
        '```'
    )
    result = AIAuditor._parse_json(raw)
    assert result is not None
    assert result["overall_score"] == 50


def test_parse_json_missing_required_keys():
    assert AIAuditor._parse_json('{"foo": "bar"}') is None


def test_parse_json_not_json():
    assert AIAuditor._parse_json("not json at all") is None


def test_parse_json_strips_leading_trailing_text():
    raw = 'Sure, here is the JSON:\n{"flaws": [], "overall_score": 10, "email_subject": "X", "opening_line": "Y"}\nHope that helps!'
    result = AIAuditor._parse_json(raw)
    assert result is not None
    assert result["overall_score"] == 10


def test_temperature_is_low_for_fact_citation_task():
    # Guards against an accidental revert to a high/creative-writing-style
    # temperature — this task is "quote exact numbers/facts accurately,"
    # not creative writing, so it should stay close to deterministic.
    assert 0 <= _AI_TEMPERATURE <= 0.4


def test_parse_json_loose_valid():
    assert AIAuditor._parse_json_loose('{"unsupported": [1, 3]}') == {"unsupported": [1, 3]}


def test_parse_json_loose_strips_markdown_fence():
    result = AIAuditor._parse_json_loose('```json\n{"unsupported": []}\n```')
    assert result == {"unsupported": []}


def test_parse_json_loose_not_json_returns_none():
    assert AIAuditor._parse_json_loose("not json") is None


def test_verify_grounding_flags_unsupported_claim(monkeypatch, capsys):
    auditor = AIAuditor()
    monkeypatch.setattr(auditor, "_call_judge", lambda prompt: '{"unsupported": [2]}')

    parsed = {
        "flaws": [
            {"paragraph": "Your site scored 42/100 on mobile speed."},
            {"paragraph": "You have no mobile version of your site at all."},
        ]
    }
    auditor._verify_grounding(parsed, "SOURCE DATA: mobile speed score 42/100", "Acme")

    captured = capsys.readouterr()
    assert "grounding check flagged 1 claim" in captured.out
    assert "no mobile version" in captured.out


def test_verify_grounding_silent_when_all_supported(monkeypatch, capsys):
    auditor = AIAuditor()
    monkeypatch.setattr(auditor, "_call_judge", lambda prompt: '{"unsupported": []}')

    parsed = {"flaws": [{"paragraph": "Your site scored 42/100 on mobile speed."}]}
    auditor._verify_grounding(parsed, "SOURCE DATA: mobile speed score 42/100", "Acme")

    captured = capsys.readouterr()
    assert "grounding check flagged" not in captured.out


def test_verify_grounding_skips_when_judge_unavailable(monkeypatch, capsys):
    auditor = AIAuditor()
    monkeypatch.setattr(auditor, "_call_judge", lambda prompt: None)

    parsed = {"flaws": [{"paragraph": "Some claim."}]}
    auditor._verify_grounding(parsed, "SOURCE DATA", "Acme")

    captured = capsys.readouterr()
    assert "grounding check flagged" not in captured.out


def test_verify_grounding_noop_on_empty_flaws(monkeypatch, capsys):
    auditor = AIAuditor()
    called = []
    monkeypatch.setattr(auditor, "_call_judge", lambda prompt: called.append(1))

    auditor._verify_grounding({"flaws": []}, "SOURCE DATA", "Acme")

    assert called == []
