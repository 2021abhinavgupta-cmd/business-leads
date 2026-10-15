import inspect

from fastapi.testclient import TestClient

import app as app_module
from storage import import_store
from import_test_support import client_with_key, csv_text, make_row, seed_lead, use_temp_db

ROUTE_FUNCS = [
    "import_upload", "import_batches", "import_batch_leads", "import_restore",
    "import_skip", "import_approve", "import_unapprove", "import_draft", "import_edit",
    "import_status",
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
