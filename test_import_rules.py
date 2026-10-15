from collections import Counter

import pytest

from importer import csv_rules
from import_test_support import csv_text, empty_blockers, make_row


def _run(rows, **sets):
    return csv_rules.apply_rules(rows, empty_blockers(**sets))


def test_parse_handles_bom_quoted_commas_and_alias_headers():
    text = (
        "﻿Email,Name,Job Title,Company,Website,Country\n"
        '"jane@acme.com","Jane Doe","Chief Operating Officer, Chief Compliance Officer",'
        "Acme Bank,https://www.acme.com/about,United States\n"
    )
    rows = csv_rules.parse_csv(text)
    assert len(rows) == 1
    r = rows[0]
    assert r["email"] == "jane@acme.com"
    assert r["person_title"] == "Chief Operating Officer, Chief Compliance Officer"
    assert r["organization"] == "Acme Bank"
    assert r["domain"] == "acme.com"
    assert r["country"] == "united states"


def test_parse_discards_phone_linkedin_and_employees():
    rows = csv_rules.parse_csv(csv_text([make_row()]))
    assert set(rows[0]) == set(csv_rules.LEAD_FIELDS)


def test_parse_joins_first_and_last_name_when_there_is_no_name_column():
    rows = csv_rules.parse_csv("first_name,last_name,email\nJane,Doe,j@a.com\n")
    assert rows[0]["person_name"] == "Jane Doe"


def test_parse_rejects_a_file_without_an_email_column():
    with pytest.raises(csv_rules.CsvImportError) as exc:
        csv_rules.parse_csv("name,company\nJane,Acme\n")
    assert "email" in str(exc.value).lower()
    assert exc.value.status_code == 400


def test_parse_rejects_an_empty_file():
    with pytest.raises(csv_rules.CsvImportError):
        csv_rules.parse_csv("")


def test_parse_enforces_row_and_size_limits_with_413(monkeypatch):
    monkeypatch.setattr(csv_rules, "MAX_ROWS", 2)
    with pytest.raises(csv_rules.CsvImportError) as exc:
        csv_rules.parse_csv("email\na@a.com\nb@b.com\nc@c.com\n")
    assert exc.value.status_code == 413
    monkeypatch.setattr(csv_rules, "MAX_ROWS", 20_000)
    monkeypatch.setattr(csv_rules, "MAX_CSV_BYTES", 10)
    with pytest.raises(csv_rules.CsvImportError) as exc:
        csv_rules.parse_csv("email\n" + "a@a.com\n" * 5)
    assert exc.value.status_code == 413


def test_each_skip_reason_fires_in_priority_order():
    rows = [
        make_row(email="not-an-email"),
        make_row(email="a@a.com", country="france"),
        make_row(email="dup@d.com", domain="d.com"),
        make_row(email="dup@d.com", domain="d2.com"),
        make_row(email="old@o.com", domain="o.com"),
        make_row(email="sup@s.com", domain="s.com"),
        make_row(email="sent@t.com", domain="t.com"),
        make_row(email="new@u.com", domain="u.com"),
        make_row(email="g@treasury.gov", domain="treasury.gov"),
        make_row(email="ok@ok.com", domain="ok.com"),
    ]
    out = _run(
        rows,
        imported={"old@o.com"}, suppressed={"sup@s.com"},
        emailed={"sent@t.com"}, company_hosts={"u.com"},
    )
    assert [r["skip_reason"] for r in out] == [
        "invalid_email", "unsupported_country", None, "duplicate_in_file",
        "already_imported", "suppressed", "already_emailed",
        "company_already_emailed", "gov_domain", None,
    ]
    assert [r["status"] for r in out].count("ready") == 2


def test_india_is_a_supported_country():
    assert _run([make_row(country="india")])[0]["status"] == "ready"


def test_one_contact_per_company_prefers_seniority_then_compliance_title():
    rows = [
        make_row(email="m@x.com", domain="x.com", person_seniority="manager", person_title="Compliance Manager"),
        make_row(email="c@x.com", domain="x.com", person_seniority="c_suite", person_title="Chief Compliance Officer"),
        make_row(email="v@x.com", domain="x.com", person_seniority="vp", person_title="VP Compliance"),
    ]
    out = _run(rows)
    assert [(r["email"], r["skip_reason"]) for r in out] == [
        ("m@x.com", "second_contact"), ("c@x.com", None), ("v@x.com", "second_contact"),
    ]


def test_contact_ties_go_to_file_order():
    rows = [
        make_row(email="first@x.com", domain="x.com", person_seniority="director", person_title="Director"),
        make_row(email="second@x.com", domain="x.com", person_seniority="director", person_title="Director"),
    ]
    out = _run(rows)
    assert [r["skip_reason"] for r in out] == [None, "second_contact"]


def test_blank_domain_falls_back_to_the_email_domain():
    rows = [
        make_row(email="a@shared.org", domain=""),
        make_row(email="b@shared.org", domain="", person_seniority="manager"),
    ]
    out = _run(rows)
    assert [r["skip_reason"] for r in out] == [None, "second_contact"]


def test_recheck_only_covers_state_dependent_rules():
    lead = make_row(email="g@treasury.gov", domain="treasury.gov")
    assert csv_rules.recheck(lead, empty_blockers()) is None
    assert csv_rules.recheck(lead, empty_blockers(suppressed={"g@treasury.gov"})) == "suppressed"
    assert csv_rules.recheck(lead, empty_blockers(emailed={"g@treasury.gov"})) == "already_emailed"
    assert csv_rules.recheck(lead, empty_blockers(company_hosts={"treasury.gov"})) == "company_already_emailed"


def test_restorable_reasons():
    assert csv_rules.RESTORABLE_REASONS == {"gov_domain", "second_contact", "manual"}


def test_real_file_shape_yields_890_ready_44_gov_66_second_contacts():
    rows = []
    for i in range(890):
        rows.append(make_row(email=f"cco{i}@bank{i}.com", domain=f"bank{i}.com"))
    for i in range(66):
        rows.append(make_row(
            email=f"mgr{i}@bank{i}.com", domain=f"bank{i}.com",
            person_seniority="manager", person_title="Manager",
        ))
    for i in range(44):
        rows.append(make_row(email=f"o{i}@agency{i}.gov", domain=f"agency{i}.gov"))
    parsed = csv_rules.parse_csv(csv_text(rows))
    assert len(parsed) == 1000
    out = csv_rules.apply_rules(parsed, empty_blockers())
    counts = Counter(r["skip_reason"] or "ready" for r in out)
    assert counts == {"ready": 890, "second_contact": 66, "gov_domain": 44}
    assert all("person_phone" not in r for r in out)
