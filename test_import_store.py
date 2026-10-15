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
