import json
import re

from analyzer.ai_audit import AIAuditor, _FLAW_EMAIL_SCHEMA

LEAD = {"person_name": "Dana Cole", "organization": "Acme Dental", "industry": "Dental"}
SHOTS = [
    {"path": "/tmp/a.png", "closeup_path": "/tmp/a2.png", "page": "https://acme.test/",
     "description": "Elements must have sufficient color contrast", "impact": "serious",
     "selector": "div.secret-selector", "kind": "desktop"},
    {"path": "/tmp/b.png", "closeup_path": "/tmp/b2.png", "page": "https://acme.test/contact",
     "description": "Images must have alternate text", "impact": "critical",
     "selector": "img.hero", "kind": "mobile"},
]
GOOD = {
    "subject": "Website issues at Acme Dental",
    "headline": "2 website issues found at Acme Dental",
    "opening": "Your site lists dental services.",
    "sections": [
        {"index": 0, "title": "Hard to read text", "sentence": "Some text can be hard to read."},
        {"index": 1, "title": "Missing image text", "sentence": "Some images may not be described."},
    ],
}


def _auditor():
    return AIAuditor.__new__(AIAuditor)


def _prompt():
    return _auditor()._build_flaw_email_prompt(LEAD, "We offer dental care.", SHOTS, "Sam")


def test_prompt_has_no_dashes_and_lists_each_measured_flaw_description():
    prompt = _prompt()
    assert not re.search(r"[-\u2010-\u2015]", prompt)
    for shot in SHOTS:
        assert shot["description"].replace("-", " ") in prompt
    assert "Acme Dental" in prompt
    assert "secret-selector" not in prompt and "/tmp/" not in prompt


def test_prompt_forbids_numbers_invented_claims_and_other_flaws():
    p = _prompt().lower()
    assert "no numbers other than" in p
    assert "never invent" in p
    assert "no other flaws" in p
    assert "never mention mmga" in p
    assert "untrusted" in p
    assert "word for word" in p
    assert "2 to 6 words" in p and "50 characters" in p
    for w in ("free", "guarantee", "urgent", "cash", "winner"):
        assert w in p
    assert "never address the recipient by name" in p
    assert "links" in p and "phone" in p


def test_prompt_requires_hedged_cost_wording():
    p = _prompt()
    assert '"can"' in p and '"may"' in p


def test_parse_rejects_wrong_section_count():
    parse = AIAuditor._parse_flaw_copy
    assert parse(json.dumps(GOOD), 3) is None
    assert parse(json.dumps(GOOD), 1) is None
    assert parse(json.dumps(GOOD), 2)["headline"].startswith("2 website")


def test_parse_rejects_duplicate_or_missing_indexes():
    parse = AIAuditor._parse_flaw_copy
    dup = json.loads(json.dumps(GOOD))
    dup["sections"][1]["index"] = 0
    assert parse(json.dumps(dup), 2) is None
    off = json.loads(json.dumps(GOOD))
    off["sections"][0]["index"] = 1
    off["sections"][1]["index"] = 2
    assert parse(json.dumps(off), 2) is None
    blank = json.loads(json.dumps(GOOD))
    blank["sections"][0]["sentence"] = "   "
    assert parse(json.dumps(blank), 2) is None


def test_parse_strips_markdown_fences_and_collapses_whitespace():
    raw = "```json\n" + json.dumps({**GOOD, "subject": "  Website\n issues  "}) + "\n```"
    out = AIAuditor._parse_flaw_copy(raw, 2)
    assert out["subject"] == "Website issues"
    assert [s["index"] for s in out["sections"]] == [0, 1]


def test_generate_uses_provider_fallback_order(monkeypatch):
    auditor = _auditor()
    calls = []

    def make(name, result):
        def fn(prompt, **kw):
            calls.append((name, kw.get("schema"), kw.get("tool_name")))
            return result
        return fn

    monkeypatch.setattr(auditor, "_call_import_anthropic", make("a", None))
    monkeypatch.setattr(auditor, "_call_import_gemini", make("g", ("garbage", 0.1)))
    monkeypatch.setattr(auditor, "_call_import_openai", make("o", (json.dumps(GOOD), 0.0004)))
    out = auditor.generate_flaw_email_copy(LEAD, "text", SHOTS, "Sam", feedback=["bad claim"])
    assert [c[0] for c in calls] == ["a", "g", "o"]
    assert all(c[1] is _FLAW_EMAIL_SCHEMA for c in calls)
    assert out["ai_cost"] == 0.0004 and len(out["sections"]) == 2


def test_generate_returns_none_when_all_fail(monkeypatch):
    auditor = _auditor()
    for name in ("_call_import_anthropic", "_call_import_gemini", "_call_import_openai"):
        monkeypatch.setattr(auditor, name, lambda p, **kw: None)
    assert auditor.generate_flaw_email_copy(LEAD, "t", SHOTS, "Sam") is None
