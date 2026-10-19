from analyzer.ai_audit import AIAuditor
from importer import compose

FACTS = "Jane Doe Chief Compliance Officer Acme Bank banking Boston Massachusetts united states acme.com"
HOMEPAGE = "Acme Bank serves families across Boston and offers wealth management."
GOOD_PARAS = ["Acme Bank serves families across Boston. Vendor onboarding often still runs by hand."]


def _body(paragraphs=GOOD_PARAS, name="Jane Doe"):
    return compose.compose_body(name, paragraphs, "Kshitij Gupta")


def _checks(body, **over):
    kwargs = dict(
        company="Acme Bank", facts_text=FACTS, homepage_text=HOMEPAGE,
        source_quote="serves families across Boston",
    )
    kwargs.update(over)
    return compose.run_checks("Vendor onboarding", body, **kwargs)


def test_fixed_line_is_short_dashless_and_makes_no_false_claim():
    line = compose.FIXED_LINE
    assert len(line.split()) <= 45
    assert not any(d in line for d in ("-", "—", "–"))
    assert "no human" not in line.lower() and "without a human" not in line.lower()
    parsed = {
        "email_subject": "x", "opening_line": "",
        "flaws": [{"paragraph": compose.mask_brand(line), "source_quote": ""}],
    }
    assert AIAuditor._check_spam_trigger_words(parsed, "c") is None
    assert AIAuditor._check_ai_sounding_phrases(parsed, "c") is None
    assert AIAuditor._check_forbidden_dashes(parsed, "c") is None
    assert AIAuditor._check_number_hallucination(parsed, "", "c") is None


def test_compose_body_greeting_order_and_signoff():
    body = _body()
    assert body.startswith("Hi Jane,\n\n")
    assert body.index(GOOD_PARAS[0]) < body.index(compose.FIXED_LINE)
    assert body.endswith("\n\nKshitij Gupta")


def test_greeting_falls_back_when_the_name_is_unusable():
    assert _body(name="").startswith("Hi there,")
    assert _body(name="Dr. Jane Doe").startswith("Hi Jane,")
    assert _body(name="1234").startswith("Hi there,")


def test_a_clean_grounded_draft_has_no_warnings():
    assert _checks(_body()) == []


def test_dashes_spam_words_and_ai_phrases_are_flagged():
    assert any("dash" in w for w in _checks(_body(["A co-op bank. Is it manual?"])))
    assert any("trigger" in w.lower() for w in _checks(_body(["Get a free audit. Is it manual?"])))
    assert any("generic" in w.lower() for w in _checks(_body(["I noticed your bank. Is it manual?"])))


def test_invented_numbers_are_flagged():
    warnings = _checks(_body(["Acme Bank serves families across Boston and saves 40 hours. Manual?"]))
    assert any("40" in w for w in warnings)


def test_missing_homepage_is_flagged_as_no_evidence():
    warnings = _checks(_body(), homepage_text=None, source_quote="")
    assert any(w.startswith("No homepage evidence") for w in warnings)


def test_a_quote_that_is_not_on_the_homepage_is_flagged():
    warnings = _checks(_body(), source_quote="a sentence the page never contained")
    assert any("source quote" in w for w in warnings)


def test_edit_mode_skips_evidence_checks_but_carries_old_evidence_warnings():
    carried = ["No homepage evidence: this draft is based only on the CSV fields."]
    warnings = _checks(
        _body(), homepage_text=None, source_quote="", check_evidence=False, carried_warnings=carried,
    )
    assert warnings == carried
    assert compose.is_evidence_warning(carried[0])
    assert not compose.is_evidence_warning("Contains trigger word(s) ['free']")


def test_links_and_email_addresses_are_flagged_but_normal_drafts_are_not():
    for text in ("See https://evil.example now.", "Visit www.evil.example.", "Write to bob@evil.example."):
        assert any("link or email" in w for w in _checks(_body([text + " Manual?"])))
    assert not any("link or email" in w for w in _checks(_body()))
    assert not any("link or email" in w for w in _checks(_body(), source_quote=""))
    assert "@" not in compose.FIXED_LINE and "http" not in compose.FIXED_LINE


def test_model_sentences_about_the_brand_are_dropped():
    body = compose.compose_body(
        "Jane Roe",
        ["Reports take hours. MMGA builds automation for this. Errors follow."],
        "Kshitij Gupta",
    )
    assert "Reports take hours. Errors follow." in body
    assert body.count("MMGA") == 1  # only the fixed line introduces it


def test_model_written_greeting_is_not_doubled():
    body = compose.compose_body("Jamie Wilson", ["Hi Jamie, Reports take hours."], "K")
    assert body.count("Hi Jamie,") == 1 and "Reports take hours." in body


def test_model_written_signoff_and_closing_question_are_dropped():
    body = compose.compose_body(
        "Jamie Wilson",
        ["Reports take hours.", "Would a chat help?\n\nBest regards,\nKshitij Gupta"],
        "Kshitij Gupta",
    )
    assert "Best regards" not in body and "Would a chat help?" not in body
    assert body.count("Kshitij Gupta") == 1 and body.count("?") == 1
