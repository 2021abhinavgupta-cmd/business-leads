from fastapi.testclient import TestClient
import app as app_module


def _client(monkeypatch):
    monkeypatch.setattr(app_module.config, "API_KEY", None)
    return TestClient(app_module.app, raise_server_exceptions=False)


def test_crud_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module.db, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(app_module.db, "DB_PATH", str(tmp_path / "t.sqlite"))
    app_module.db.init_db()
    client = _client(monkeypatch)

    r = client.post("/api/social/drafts", json={
        "company": "Acme", "platform": "instagram", "handle": "acme",
        "profile_url": "https://instagram.com/acme", "channel": "dm",
        "target": "acme", "body": "hey", "issues": [],
    })
    assert r.status_code == 200, r.text
    rid = r.json()["id"]

    r = client.get("/api/social/drafts")
    assert r.status_code == 200
    assert any(d["id"] == rid for d in r.json()["drafts"])

    r = client.delete(f"/api/social/drafts/{rid}")
    assert r.status_code == 200 and r.json()["deleted"] is True
    assert all(d["id"] != rid for d in client.get("/api/social/drafts").json()["drafts"])


def test_send_calls_sender(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module.db, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(app_module.db, "DB_PATH", str(tmp_path / "t.sqlite"))
    sent = {}

    class _FakeSender:
        def send_email(self, to_email, subject, body, **kw):
            sent.update(to_email=to_email, subject=subject, body=body)
            return "msg-id-123"

    monkeypatch.setattr(app_module, "get_sender", lambda: _FakeSender())
    client = _client(monkeypatch)
    r = client.post("/api/social/send", json={
        "company": "Acme", "target": "owner@acme.com", "subject": "Your IG went quiet", "body": "Hi",
    })
    assert r.status_code == 200, r.text
    assert r.json()["sent"] is True
    assert sent["to_email"] == "owner@acme.com"


def test_routes_are_api_key_gated():
    import inspect
    for fn in (app_module.social_drafts_list, app_module.social_drafts_create,
               app_module.social_drafts_delete, app_module.social_send):
        src = inspect.getsource(fn)
        assert "require_api_key" in src
        assert "rate_limit" in src
