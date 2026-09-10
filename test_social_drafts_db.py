from storage import db


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.sqlite"))
    db.init_db()
    return db


def test_log_and_get_roundtrip(tmp_path, monkeypatch):
    d = _fresh(tmp_path, monkeypatch)
    rid = d.log_social_draft(
        "Acme", "instagram", "acme", "https://instagram.com/acme",
        "dm", "acme", "", "hey your IG went quiet",
        [{"severity": "high", "label": "Inactive account", "detail": "quiet"}],
    )
    assert isinstance(rid, int)
    rows = d.get_social_drafts()
    assert len(rows) == 1
    r = rows[0]
    assert r["company"] == "Acme"
    assert r["platform"] == "instagram"
    assert r["channel"] == "dm"
    assert r["subject"] == ""
    assert r["issues"] == [{"severity": "high", "label": "Inactive account", "detail": "quiet"}]


def test_get_is_newest_first(tmp_path, monkeypatch):
    d = _fresh(tmp_path, monkeypatch)
    a = d.log_social_draft("A", "instagram", "a", "u", "email", "a@x.com", "S1", "B1", [])
    b = d.log_social_draft("B", "youtube", "b", "u", "email", "b@x.com", "S2", "B2", [])
    rows = d.get_social_drafts()
    assert [r["id"] for r in rows] == [b, a]


def test_delete(tmp_path, monkeypatch):
    d = _fresh(tmp_path, monkeypatch)
    rid = d.log_social_draft("A", "instagram", "a", "u", "email", "a@x.com", "S", "B", [])
    d.delete_social_draft(rid)
    assert d.get_social_drafts() == []


def test_bad_issues_json_decodes_to_empty_list(tmp_path, monkeypatch):
    d = _fresh(tmp_path, monkeypatch)
    rid = d.log_social_draft("A", "instagram", "a", "u", "email", "a@x.com", "S", "B", [])
    import sqlite3
    conn = sqlite3.connect(d.DB_PATH)
    conn.execute("UPDATE social_drafts SET issues_json = 'not json' WHERE id = ?", (rid,))
    conn.commit()
    conn.close()
    assert d.get_social_drafts()[0]["issues"] == []
