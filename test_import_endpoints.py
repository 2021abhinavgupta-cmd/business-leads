import inspect

from fastapi.testclient import TestClient

import app as app_module
from storage import import_store
from import_test_support import client_with_key, csv_text, make_row, seed_lead, use_temp_db

ROUTE_FUNCS = [
    "import_upload", "import_batches", "import_batch_leads", "import_restore",
    "import_skip", "import_approve", "import_unapprove", "import_draft", "import_edit",
    "import_status", "import_audit_start", "import_audit_progress", "import_audit_cancel",
    "import_preview", "import_design_edit",
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


# --- designed drafts: list, preview, design edit (designed-flaw email, Task 7) ---

import base64  # noqa: E402
import os  # noqa: E402

import pytest  # noqa: E402

from importer import drafting as import_drafting, email_html, email_images  # noqa: E402
from import_test_support import seed_designed_lead  # noqa: E402

def test_leads_list_replaces_html_body_with_is_designed(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    designed = seed_designed_lead(tmp_path)
    batch_id = import_store.get_lead(designed)["batch_id"]
    text_batch = import_store.create_batch("t.csv", [{**make_row(email="t@t.com"), "status": "ready"}])
    leads = client.get(f"/api/import/batches/{batch_id}/leads").json()["leads"]
    assert len(leads) == 1 and leads[0]["is_designed"] is True and "html_body" not in leads[0]
    assert leads[0]["design_json"]["sections"] and leads[0]["subject"] and leads[0]["body"]
    text = client.get(f"/api/import/batches/{text_batch}/leads").json()["leads"][0]
    assert text["is_designed"] is False and "html_body" not in text
    for key in ("id", "status", "subject", "body", "review_warnings", "source_quote", "email", "audit_status"):
        assert key in text
    # every lead returned to the drawer is the same public shape
    r = client.post(f"/api/import/leads/{designed}/approve", json={"acknowledge_warnings": True})
    assert r.status_code == 200 and "html_body" not in r.json()["lead"] and r.json()["lead"]["is_designed"]


def test_preview_embeds_the_exact_prepared_image_bytes(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    lead_id = seed_designed_lead(tmp_path, 3)
    shots = import_store.get_lead(lead_id)["flaw_shots"]
    r = client.get(f"/api/import/leads/{lead_id}/preview")
    assert r.status_code == 200, r.text
    html = r.json()["html"]
    for cid, data in email_images.prepare_all(shots):
        uri = "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
        assert f'src="{uri}"' in html, cid
    assert "cid:" not in html and email_html.FOOTER_MARKER not in html
    assert "Footer is added when the email is sent" in html
    # the stored email is unchanged: the sender still needs its cid references
    assert import_store.get_lead(lead_id)["html_body"].count("cid:flaw") == 3


def test_preview_errors(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    assert client.get("/api/import/leads/999/preview").status_code == 404
    text = seed_lead(status="drafted", subject="S", body="B")
    assert client.get(f"/api/import/leads/{text}/preview").status_code == 409
    lead_id = seed_designed_lead(tmp_path)
    os.remove(import_store.get_lead(lead_id)["flaw_shots"][1]["path"])
    r = client.get(f"/api/import/leads/{lead_id}/preview")
    assert r.status_code == 409 and "screenshot" in r.json()["detail"].lower()


def test_preview_refuses_when_the_screenshots_changed_since_drafting(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    lead_id = seed_designed_lead(tmp_path)
    shots = import_store.get_lead(lead_id)["flaw_shots"]
    import_store.update_lead(lead_id, flaw_shots=list(reversed(shots)))  # a re-audit reordered them
    r = client.get(f"/api/import/leads/{lead_id}/preview")
    assert r.status_code == 409 and "draft" in r.json()["detail"].lower()


def _edit_body(n=2, **over):
    body = {
        "subject": "Problems on the Acme site",
        "opening": "A visitor sees these problems first.",
        "sections": [{"index": i, "sentence": f"Edited sentence {'ABCDE'[i]} can cost visitors."} for i in range(n)],
    }
    body.update(over)
    return body


def test_design_edit_rerenders_and_rejects_html_in_sentences(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    lead_id = seed_designed_lead(tmp_path, status="approved", review_warnings=["Fact check: old"])
    before = import_store.get_lead(lead_id)

    r = client.put(f"/api/import/leads/{lead_id}/design", json=_edit_body())
    assert r.status_code == 200, r.text
    out = r.json()["lead"]
    assert out["status"] == "drafted" and out["is_designed"] and "html_body" not in out
    stored = import_store.get_lead(lead_id)
    assert stored["subject"] == "Problems on the Acme site"
    assert "Edited sentence A can cost visitors." in stored["html_body"]
    assert "Edited sentence B can cost visitors." in stored["body"]
    assert "A visitor sees these problems first." in stored["html_body"]
    assert stored["html_body"].count("cid:flaw") == 2
    design = stored["design_json"]
    assert design["opening"] == "A visitor sees these problems first."
    assert [s["sentence"] for s in design["sections"]] == [
        "Edited sentence A can cost visitors.", "Edited sentence B can cost visitors."]
    # headline and titles are not editable and are kept
    assert design["headline"] == before["design_json"]["headline"]
    assert [s["title"] for s in design["sections"]] == [s["title"] for s in before["design_json"]["sections"]]
    assert design["shot_fingerprints"] == before["design_json"]["shot_fingerprints"]
    assert stored["review_warnings"] == []

    for bad in (
        _edit_body(sections=[{"index": 0, "sentence": "Bad <b>bold</b>."}, {"index": 1, "sentence": "Ok."}]),
        _edit_body(opening="<script>x</script>"),
        _edit_body(subject="A > B"),
    ):
        r = client.put(f"/api/import/leads/{lead_id}/design", json=bad)
        assert r.status_code == 422, bad
    assert import_store.get_lead(lead_id)["html_body"] == stored["html_body"]


@pytest.mark.parametrize("bad", [
    _edit_body(n=1),
    _edit_body(n=3),
    _edit_body(sections=[{"index": 1, "sentence": "One."}, {"index": 0, "sentence": "Two."}]),
    _edit_body(sections=[{"index": 0, "sentence": "One."}, {"index": 0, "sentence": "Two."}]),
    _edit_body(subject="  "),
    _edit_body(opening=""),
    _edit_body(sections=[{"index": 0, "sentence": " "}, {"index": 1, "sentence": "Two."}]),
])
def test_design_edit_rejects_wrong_sections_and_empty_values(monkeypatch, tmp_path, bad):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    lead_id = seed_designed_lead(tmp_path)
    before = import_store.get_lead(lead_id)
    assert client.put(f"/api/import/leads/{lead_id}/design", json=bad).status_code == 422
    after = import_store.get_lead(lead_id)
    assert after["html_body"] == before["html_body"] and after["subject"] == before["subject"]


def test_design_edit_cleans_dashes_and_warns_on_digits_links_and_subject(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    lead_id = seed_designed_lead(tmp_path)
    body = _edit_body(
        subject="Problems—",
        sections=[{"index": 0, "sentence": "A first-time visitor leaves after 40 seconds."},
                  {"index": 1, "sentence": "See www.acme.com and ask MMGA."}],
    )
    r = client.put(f"/api/import/leads/{lead_id}/design", json=body)
    assert r.status_code == 200, r.text
    stored = import_store.get_lead(lead_id)
    assert stored["subject"] == "Problems"
    assert stored["design_json"]["sections"][0]["sentence"] == "A first time visitor leaves after 40 seconds."
    warnings = " ".join(stored["review_warnings"]).lower()
    assert "number" in warnings and "link" in warnings and "mmga" in warnings and "subject" in warnings
    assert not any(w.startswith("Fact check") for w in stored["review_warnings"])  # a human wrote it


def test_design_edit_requires_a_designed_lead(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    assert client.put("/api/import/leads/999/design", json=_edit_body()).status_code == 404
    text = seed_lead(status="drafted", subject="S", body="B")
    r = client.put(f"/api/import/leads/{text}/design", json=_edit_body())
    assert r.status_code == 409
    for status in ("sent", "skipped", "ready"):
        lead_id = seed_designed_lead(tmp_path, status=status)
        assert client.put(f"/api/import/leads/{lead_id}/design", json=_edit_body()).status_code == 409, status
    changed = seed_designed_lead(tmp_path)
    shots = import_store.get_lead(changed)["flaw_shots"]
    import_store.update_lead(changed, flaw_shots=shots[:1] + shots[:1])
    assert client.put(f"/api/import/leads/{changed}/design", json=_edit_body()).status_code == 409


def test_text_edit_of_a_designed_lead_is_refused(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    lead_id = seed_designed_lead(tmp_path)
    before = import_store.get_lead(lead_id)
    r = client.put(f"/api/import/leads/{lead_id}", json={"subject": "New", "body": "Hi Jane,\n\nNew"})
    assert r.status_code == 409 and "design" in r.json()["detail"].lower()
    assert import_store.get_lead(lead_id)["body"] == before["body"]


# --- review fixes: screenshot fingerprints and render failures ---

from PIL import Image  # noqa: E402


def _overwrite_pixels(lead_id, index=0):
    path = import_store.get_lead(lead_id)["flaw_shots"][index]["path"]
    Image.new("RGB", (900, 600), (10, 120, 30)).save(path)  # same path, new image


def test_preview_and_design_edit_refuse_a_shot_overwritten_at_the_same_path(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    lead_id = seed_designed_lead(tmp_path)
    assert client.get(f"/api/import/leads/{lead_id}/preview").status_code == 200
    before = import_store.get_lead(lead_id)
    _overwrite_pixels(lead_id)
    assert [s["path"] for s in import_store.get_lead(lead_id)["flaw_shots"]] == [
        s["path"] for s in before["flaw_shots"]]
    for r in (client.get(f"/api/import/leads/{lead_id}/preview"),
              client.put(f"/api/import/leads/{lead_id}/design", json=_edit_body())):
        assert r.status_code == 409
        # the file no longer has the hash the audit recorded: re-audit, then redraft
        assert r.json()["detail"] == import_drafting.SHOTS_CHANGED_ON_DISK
    assert import_store.get_lead(lead_id)["html_body"] == before["html_body"]


def test_preview_and_design_edit_refuse_a_changed_description(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    lead_id = seed_designed_lead(tmp_path)
    shots = import_store.get_lead(lead_id)["flaw_shots"]
    shots[1]["description"] = "A different flaw on the same screenshot"
    import_store.update_lead(lead_id, flaw_shots=shots)  # a discarded re-audit kept the files, not the words
    assert client.get(f"/api/import/leads/{lead_id}/preview").status_code == 409
    assert client.put(f"/api/import/leads/{lead_id}/design", json=_edit_body()).status_code == 409


def test_design_edit_with_a_missing_shot_file_is_409_not_500(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    lead_id = seed_designed_lead(tmp_path)
    os.remove(import_store.get_lead(lead_id)["flaw_shots"][0]["path"])
    r = client.put(f"/api/import/leads/{lead_id}/design", json=_edit_body())
    assert r.status_code == 409 and "missing" in r.json()["detail"]


def test_design_edit_render_failure_is_a_message_not_a_500(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    lead_id = seed_designed_lead(tmp_path)
    before = import_store.get_lead(lead_id)

    def boom(*a, **kw):
        raise ValueError("bad template")

    monkeypatch.setattr(email_html, "render", boom)
    r = client.put(f"/api/import/leads/{lead_id}/design", json=_edit_body())
    assert r.status_code == 422 and "could not be built" in r.json()["detail"]
    assert import_store.get_lead(lead_id)["html_body"] == before["html_body"]


def test_leads_listing_filters_by_audit_status(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    client = client_with_key(monkeypatch)
    rows = [make_row(email="f@f.com", domain="f.com"), make_row(email="n@n.com", domain="n.com")]
    batch_id = client.post("/api/import/upload", json={"filename": "f.csv", "csv_text": csv_text(rows)}).json()["batch_id"]
    leads = client.get(f"/api/import/batches/{batch_id}/leads").json()["leads"]
    assert len(leads) == 2
    import_store.update_lead(leads[0]["id"], audit_status="failed")
    body = client.get(f"/api/import/batches/{batch_id}/leads", params={"audit_status": "failed"}).json()
    assert [lead["id"] for lead in body["leads"]] == [leads[0]["id"]]
