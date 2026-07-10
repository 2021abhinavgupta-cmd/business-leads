"""
Unit tests for AIAuditor._parse_json — no network, no API keys required.
Runs on every push; this is the one real regression guard in the suite.
"""

from analyzer.ai_audit import AIAuditor


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
