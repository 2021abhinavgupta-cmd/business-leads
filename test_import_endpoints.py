import inspect

from fastapi.testclient import TestClient

import app as app_module
from storage import import_store
from import_test_support import client_with_key, csv_text, make_row, seed_lead, use_temp_db

ROUTE_FUNCS = [
    "import_upload", "import_batches", "import_batch_leads", "import_restore",
    "import_skip", "import_approve", "import_unapprove", "import_draft", "import_edit",
    "import_status", "import_audit_start", "import_audit_progress", "import_audit_cancel",
]


def test_every_import_route_is_key_gated_and_rate_limited():
    for name in ROUTE_FUNCS:
        src = inspect.getsource(getattr(app_module, name))
        assert "require_api_key" in src, name
        assert "rate_limit" in src, name


def test_import_routes_need_the_api_key(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client_with_key(monkeypatch)   # sets a real API_KEY for the app
    bare = TestClient(app_module.app, raise_server_exceptions=False)
    assert bare.get("/api/import/batches").status_code == 401
    assert bare.post("/api/import/upload", json={"csv_text": "email\na@a.com\n"}).status_code == 401


def test_upload_list_and_restore_flow(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    rows = [make_row(email="a@a.com", domain="a.com"), make_row(email="g@b.gov", domain="b.gov")]
    r = client.post("/api/import/upload", json={"filename": "f.csv", "csv_text": csv_text(rows)})
    assert r.status_code == 200, r.text
    summary = r.json()
    assert summary["ready"] == 1 and summary["skip_breakdown"] == {"gov_domain": 1}

    r = client.get("/api/import/batches")
    assert r.json()["batches"][0]["id"] == summary["batch_id"]

    r = client.get(f"/api/import/batches/{summary['batch_id']}/leads", params={"status": "skipped"})
    body = r.json()
    assert body["total"] == 1 and "facets" in body
    gov_id = body["leads"][0]["id"]

    r = client.post(f"/api/import/leads/{gov_id}/restore")
    assert r.status_code == 200 and r.json()["lead"]["status"] == "ready"
    assert client.post(f"/api/import/leads/{gov_id}/skip").json()["lead"]["skip_reason"] == "manual"


def test_upload_errors_map_to_http_status(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    assert client.post("/api/import/upload", json={"csv_text": "name\nx\n"}).status_code == 400
    monkeypatch.setattr(app_module.import_service.csv_rules, "MAX_ROWS", 1)
    r = client.post("/api/import/upload", json={"csv_text": "email\na@a.com\nb@b.com\n"})
    assert r.status_code == 413


def test_leads_listing_caps_the_page_size(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    lead_id = seed_lead()
    batch_id = import_store.get_lead(lead_id)["batch_id"]
    r = client.get(f"/api/import/batches/{batch_id}/leads", params={"limit": 5000})
    assert r.status_code == 422


def test_unknown_lead_is_404_and_approve_flow(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    assert client.post("/api/import/leads/999/restore").status_code == 404
    lead_id = seed_lead(status="drafted", subject="S", body="B", review_warnings=["w"])
    r = client.post(f"/api/import/leads/{lead_id}/approve", json={"acknowledge_warnings": False})
    assert r.status_code == 409 and r.json()["detail"]["warnings"] == ["w"]
    r = client.post(f"/api/import/leads/{lead_id}/approve", json={"acknowledge_warnings": True})
    assert r.status_code == 200 and r.json()["lead"]["status"] == "approved"
    r = client.post(f"/api/import/leads/{lead_id}/unapprove")
    assert r.json()["lead"]["status"] == "drafted"


# --- audit endpoints (designed-flaw email, Task 4) ---------------------------

def _batch_of(n, **over):
    rows = [{**make_row(email=f"p{i}@a{i}.com", domain=f"a{i}.com", **over), "status": "ready"} for i in range(n)]
    batch_id = import_store.create_batch("f.csv", rows)
    return batch_id, [l["id"] for l in import_store.list_leads(batch_id)["leads"]]


def _spy_start(monkeypatch, result=True):
    calls = []

    def fake(lead_ids, **kw):
        calls.append(list(lead_ids))
        return result
    monkeypatch.setattr(app_module.audit_runner, "start_audit", fake)
    return calls


def test_audit_start_selects_next_ready_leads_without_a_status(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    calls = _spy_start(monkeypatch)
    batch_id, ids = _batch_of(4)
    import_store.update_lead(ids[0], audit_status="done")
    r = client.post(f"/api/import/batches/{batch_id}/audit", json={"limit": 2})
    assert r.status_code == 200 and r.json() == {"started": True, "queued": 2}
    assert calls == [[ids[1], ids[2]]]
    # default limit is 25: everything left is selected
    calls.clear()
    r = client.post(f"/api/import/batches/{batch_id}/audit", json={})
    assert r.json() == {"started": True, "queued": 3} and calls == [[ids[1], ids[2], ids[3]]]


def test_audit_start_with_explicit_ids_keeps_only_this_batchs_leads(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    calls = _spy_start(monkeypatch)
    batch_id, ids = _batch_of(2)
    _, foreign = _batch_of(1)
    r = client.post(f"/api/import/batches/{batch_id}/audit", json={"lead_ids": [ids[1], foreign[0], 99999]})
    assert r.json() == {"started": True, "queued": 1} and calls == [[ids[1]]]
    assert client.post(f"/api/import/batches/{batch_id}/audit", json={"lead_ids": [1] * 201}).status_code == 422


def test_audit_start_rejects_limit_over_200(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    calls = _spy_start(monkeypatch)
    batch_id, _ = _batch_of(1)
    for bad in (201, 0, -1):
        r = client.post(f"/api/import/batches/{batch_id}/audit", json={"limit": bad})
        assert r.status_code == 422, bad
    assert client.post(f"/api/import/batches/{batch_id}/audit", json={"limit": 200}).status_code == 200
    assert len(calls) == 1


def test_audit_start_returns_started_false_when_busy(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    _spy_start(monkeypatch, result=False)
    batch_id, _ = _batch_of(2)
    r = client.post(f"/api/import/batches/{batch_id}/audit", json={})
    assert r.status_code == 200 and r.json() == {"started": False, "queued": 0}


def test_audit_start_unknown_batch_is_404(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    calls = _spy_start(monkeypatch)
    assert client.post("/api/import/batches/999/audit", json={}).status_code == 404
    assert calls == []


def test_audit_endpoints_require_the_api_key(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client_with_key(monkeypatch)
    bare = TestClient(app_module.app, raise_server_exceptions=False)
    assert bare.post("/api/import/batches/1/audit", json={}).status_code == 401
    assert bare.get("/api/import/audit/progress").status_code == 401
    assert bare.post("/api/import/audit/cancel").status_code == 401
    for name in ("import_audit_start", "import_audit_progress", "import_audit_cancel"):
        src = inspect.getsource(getattr(app_module, name))
        assert "require_api_key" in src and "rate_limit" in src, name


def test_audit_progress_and_cancel_shapes(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    shape = {"running": False, "total": 0, "done": 0, "no_flaws": 0, "failed": 0, "current": None}
    monkeypatch.setattr(app_module.audit_runner, "progress", lambda: dict(shape))
    cancelled = []
    monkeypatch.setattr(app_module.audit_runner, "cancel", lambda: cancelled.append(1))
    assert client.get("/api/import/audit/progress").json() == shape
    r = client.post("/api/import/audit/cancel")
    assert r.status_code == 200 and r.json() == shape and cancelled == [1]


def test_audit_explicit_ids_are_limited_to_auditable_statuses(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    calls = _spy_start(monkeypatch)
    batch_id, ids = _batch_of(5)
    for lead_id, status in zip(ids[1:4], ("skipped", "approved", "sent")):
        import_store.update_lead(lead_id, status=status)
    import_store.update_lead(ids[4], status="drafted")
    r = client.post(f"/api/import/batches/{batch_id}/audit", json={"lead_ids": ids})
    assert calls == [[ids[0], ids[4]]]
    assert r.json() == {"started": True, "queued": 2}


def test_normal_startup_resets_stale_audit_state_exactly_once(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(app_module.audit_runner, "reset_stale_running", lambda: calls.append(1) or 3)
    with TestClient(app_module.app):
        pass
    assert calls == [1]


def test_failing_startup_reset_is_logged_and_does_not_stop_the_server(monkeypatch, tmp_path, capsys):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)

    def boom():
        raise RuntimeError("db locked")
    monkeypatch.setattr(app_module.audit_runner, "reset_stale_running", boom)
    with TestClient(app_module.app, headers=client.headers) as c:
        assert c.get("/api/import/audit/progress").status_code == 200
    out = capsys.readouterr().out
    assert "Could not reset stale audit state" in out and "db locked" in out
