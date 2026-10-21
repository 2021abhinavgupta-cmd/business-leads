import sqlite3

import pytest

from storage import db, import_store
from import_test_support import make_row, use_temp_db


def _ready(**over):
    row = make_row(**over)
    row.update(status="ready", skip_reason=None)
    return row


def test_init_db_creates_import_tables_and_bumps_to_v14(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    conn = sqlite3.connect(db.DB_PATH)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert {"import_batches", "import_leads"} <= tables
    assert version >= 14


def test_a_v13_database_upgrades_without_losing_history(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    db.log_email("Acme", "acme.com", "a@acme.com", "us@x.com", "S", "B")
    conn = sqlite3.connect(db.DB_PATH)
    conn.execute("DROP TABLE import_leads")
    conn.execute("DROP TABLE import_batches")
    conn.execute("PRAGMA user_version = 13")
    conn.commit()
    conn.close()
    db.init_db()
    assert import_store.list_batches() == []
    assert len(db.get_email_history()) == 1


def test_create_batch_counts_and_list_batches(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    skipped = make_row(email="g@x.gov")
    skipped.update(status="skipped", skip_reason="gov_domain")
    batch_id = import_store.create_batch("f.csv", [_ready(), skipped])
    batches = import_store.list_batches()
    assert batches[0]["id"] == batch_id
    assert (batches[0]["total_rows"], batches[0]["ready_count"], batches[0]["skipped_count"]) == (2, 1, 1)
    assert batches[0]["status_counts"] == {"ready": 1, "skipped": 1}


def test_list_leads_filters_search_and_paging(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    rows = [
        _ready(email="a@a.com", organization="Alpha Bank", industry="banking", state="Ohio"),
        _ready(email="b@b.com", organization="Beta Hospital", industry="hospitals", state="Texas"),
        _ready(email="c@c.com", organization="Gamma Bank", industry="banking", state="Texas"),
    ]
    batch_id = import_store.create_batch("f.csv", rows)
    assert import_store.list_leads(batch_id)["total"] == 3
    assert import_store.list_leads(batch_id, industry="banking")["total"] == 2
    assert import_store.list_leads(batch_id, state="Texas", industry="banking")["total"] == 1
    assert [l["email"] for l in import_store.list_leads(batch_id, q="hospital")["leads"]] == ["b@b.com"]
    page = import_store.list_leads(batch_id, limit=2, offset=2)
    assert page["total"] == 3 and len(page["leads"]) == 1
    assert import_store.facets(batch_id)["industry"] == {"banking": 2, "hospitals": 1}


def test_update_lead_whitelist_and_json_warnings(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    batch_id = import_store.create_batch("f.csv", [_ready()])
    lead_id = import_store.list_leads(batch_id)["leads"][0]["id"]
    import_store.update_lead(lead_id, status="drafted", subject="S", body="B", review_warnings=["w1", "w2"])
    lead = import_store.get_lead(lead_id)
    assert lead["status"] == "drafted" and lead["review_warnings"] == ["w1", "w2"]
    with pytest.raises(ValueError):
        import_store.update_lead(lead_id, email="evil@x.com")
    assert import_store.get_lead(999) is None


def test_load_blockers_reads_suppressions_history_and_sent_hosts(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    db.add_suppression("Out@Out.com", "unsub")
    db.log_email("Sent Co", "https://www.sentco.com/", "Hi@SentCo.com", "us@x.com", "S", "B")
    import_store.create_batch("f.csv", [_ready(email="Prev@Prev.com")])
    blockers = import_store.load_blockers()
    assert "out@out.com" in blockers["suppressed"]
    assert "hi@sentco.com" in blockers["emailed"]
    assert "sentco.com" in blockers["company_hosts"]
    assert "prev@prev.com" in blockers["imported"]


_V15_COLUMNS = {"audit_status", "audit_error", "audited_at", "flaw_shots", "html_body", "design_json"}

_V14_IMPORT_LEADS = """
CREATE TABLE import_leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL,
    person_name TEXT, person_title TEXT, person_seniority TEXT, email TEXT NOT NULL,
    organization TEXT, domain TEXT, industry TEXT, city TEXT, state TEXT, country TEXT,
    status TEXT NOT NULL, skip_reason TEXT, subject TEXT, body TEXT, review_warnings TEXT,
    source_quote TEXT, drafted_at DATETIME, sent_at DATETIME, history_id INTEGER, error TEXT,
    FOREIGN KEY (batch_id) REFERENCES import_batches(id)
)
"""


def _lead_columns():
    conn = sqlite3.connect(db.DB_PATH)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(import_leads)")}
    conn.close()
    return cols


def _downgrade_to_v14_with_one_row():
    """Rebuild import_leads exactly as v14 shipped it, holding one old row."""
    conn = sqlite3.connect(db.DB_PATH)
    conn.execute("DROP TABLE import_leads")
    conn.execute(_V14_IMPORT_LEADS)
    conn.execute("INSERT INTO import_batches (filename, total_rows, ready_count, skipped_count) VALUES ('old.csv', 1, 1, 0)")
    conn.execute(
        "INSERT INTO import_leads (batch_id, email, organization, status, subject) "
        "VALUES (1, 'old@acme.com', 'Old Co', 'drafted', 'Old subject')"
    )
    conn.execute("PRAGMA user_version = 14")
    conn.commit()
    conn.close()


def test_v15_adds_audit_and_design_columns(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    conn = sqlite3.connect(db.DB_PATH)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert _V15_COLUMNS <= _lead_columns()
    assert version == 15


def test_v14_database_migrates_to_v15_keeping_rows(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    _downgrade_to_v14_with_one_row()
    assert not (_V15_COLUMNS & _lead_columns())
    db.init_db()
    assert _V15_COLUMNS <= _lead_columns()
    lead = import_store.get_lead(1)
    assert (lead["email"], lead["organization"], lead["subject"]) == ("old@acme.com", "Old Co", "Old subject")
    conn = sqlite3.connect(db.DB_PATH)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 15
    conn.close()


def test_existing_rows_get_audit_status_none_and_new_rows_default_to_none(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    _downgrade_to_v14_with_one_row()
    db.init_db()
    assert import_store.get_lead(1)["audit_status"] == "none"
    batch_id = import_store.create_batch("new.csv", [_ready(email="n@n.com")])
    assert import_store.list_leads(batch_id)["leads"][0]["audit_status"] == "none"
    assert import_store.list_leads(1, audit_status="none")["total"] == 1


def test_update_lead_round_trips_flaw_shots_and_design_json_as_decoded_values(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    batch_id = import_store.create_batch("f.csv", [_ready()])
    lead_id = import_store.list_leads(batch_id)["leads"][0]["id"]
    blank = import_store.get_lead(lead_id)
    assert blank["flaw_shots"] == [] and blank["design_json"] == {}
    shots = [{"title": "Slow", "path": "a.png"}, {"title": "Broken form", "path": "b.png"}]
    design = {"accent": "#c00", "layout": "hero"}
    import_store.update_lead(
        lead_id, flaw_shots=shots, design_json=design, audit_status="done", audit_error=None,
        audited_at="2026-10-21 10:00:00", html_body="<p>hi</p>",
    )
    for lead in (import_store.get_lead(lead_id), import_store.list_leads(batch_id)["leads"][0]):
        assert lead["flaw_shots"] == shots
        assert lead["design_json"] == design
        assert lead["audit_status"] == "done" and lead["html_body"] == "<p>hi</p>"
        assert lead["audited_at"] == "2026-10-21 10:00:00"
    import_store.update_lead(lead_id, flaw_shots='["x"]', design_json='{"k": 1}')
    lead = import_store.get_lead(lead_id)
    assert lead["flaw_shots"] == ["x"] and lead["design_json"] == {"k": 1}


def test_list_leads_filters_by_audit_status(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    batch_id = import_store.create_batch(
        "f.csv", [_ready(email="a@a.com"), _ready(email="b@b.com"), _ready(email="c@c.com")]
    )
    ids = [l["id"] for l in import_store.list_leads(batch_id)["leads"]]
    import_store.update_lead(ids[0], audit_status="done")
    import_store.update_lead(ids[1], audit_status="failed")
    assert [l["email"] for l in import_store.list_leads(batch_id, audit_status="done")["leads"]] == ["a@a.com"]
    assert import_store.list_leads(batch_id, audit_status="none")["total"] == 1
    assert import_store.list_leads(batch_id)["total"] == 3
    assert import_store.facets(batch_id)["audit_status"] == {"done": 1, "failed": 1, "none": 1}


def test_next_leads_to_audit_selects_ready_unaudited_in_id_order(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    rows = [_ready(email=f"{n}@a.com", domain="a.com") for n in "abcde"]
    skipped = make_row(email="s@a.com")
    skipped.update(status="skipped", skip_reason="manual")
    batch_id = import_store.create_batch("f.csv", rows + [skipped])
    other = import_store.create_batch("g.csv", [_ready(email="z@z.com", domain="z.com")])
    ids = [l["id"] for l in import_store.list_leads(batch_id)["leads"]]
    import_store.update_lead(ids[0], audit_status="done")
    import_store.update_lead(ids[1], status="drafted")
    assert import_store.next_leads_to_audit(batch_id, 2) == [ids[2], ids[3]]
    assert import_store.next_leads_to_audit(batch_id, 50) == [ids[2], ids[3], ids[4]]
    assert import_store.next_leads_to_audit(other, 5) != []


def test_batch_exists_and_lead_ids_in_batch(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    batch_id = import_store.create_batch("f.csv", [_ready(email="a@a.com", domain="a.com")])
    other = import_store.create_batch("g.csv", [_ready(email="z@z.com", domain="z.com")])
    mine = import_store.list_leads(batch_id)["leads"][0]["id"]
    theirs = import_store.list_leads(other)["leads"][0]["id"]
    assert import_store.batch_exists(batch_id) and not import_store.batch_exists(999)
    assert import_store.lead_ids_in_batch(batch_id, [theirs, mine, mine, 999]) == [mine]
    assert import_store.lead_ids_in_batch(batch_id, []) == []
