import pytest

from importer import service
from storage import db, import_store
from import_test_support import csv_text, make_row, seed_lead, use_temp_db


def test_upload_stores_batch_and_reports_breakdown(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    monkeypatch.setattr(service.config, "MAILING_ADDRESS", "")
    rows = [
        make_row(email="a@a.com", domain="a.com"),
        make_row(email="b@b.gov", domain="b.gov"),
        make_row(email="bad"),
    ]
    summary = service.upload("x.csv", csv_text(rows))
    assert (summary["total"], summary["ready"], summary["skipped"]) == (3, 1, 2)
    assert summary["skip_breakdown"] == {"gov_domain": 1, "invalid_email": 1}
    assert summary["us_ready"] == 1
    assert summary["mailing_address_set"] is False
    assert import_store.list_leads(summary["batch_id"])["leads"][0]["status"] == "ready"


def test_uploading_a_bad_file_raises_a_flow_error_and_stores_nothing(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    with pytest.raises(service.FlowError) as exc:
        service.upload("x.csv", "name,company\nJane,Acme\n")
    assert exc.value.status_code == 400
    assert import_store.list_batches() == []


def test_reuploading_the_same_file_skips_everything_as_already_imported(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    text = csv_text([make_row(email="a@a.com", domain="a.com")])
    service.upload("x.csv", text)
    second = service.upload("x.csv", text)
    assert second["ready"] == 0
    assert second["skip_breakdown"] == {"already_imported": 1}


def test_restore_a_government_lead_then_it_is_ready(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    summary = service.upload("x.csv", csv_text([make_row(email="g@b.gov", domain="b.gov")]))
    lead = import_store.list_leads(summary["batch_id"])["leads"][0]
    restored = service.restore(lead["id"])
    assert restored["status"] == "ready" and restored["skip_reason"] is None


def test_restore_rechecks_state_that_changed_after_import(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    summary = service.upload("x.csv", csv_text([make_row(email="g@b.gov", domain="b.gov")]))
    lead_id = import_store.list_leads(summary["batch_id"])["leads"][0]["id"]
    db.add_suppression("g@b.gov", "unsub")
    with pytest.raises(service.FlowError) as exc:
        service.restore(lead_id)
    assert exc.value.status_code == 409
    assert import_store.get_lead(lead_id)["skip_reason"] == "suppressed"


def test_restore_refuses_a_non_restorable_skip(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    lead_id = seed_lead(status="skipped", skip_reason="invalid_email")
    with pytest.raises(service.FlowError) as exc:
        service.restore(lead_id)
    assert exc.value.status_code == 409


def test_manual_skip_is_restorable(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    lead_id = seed_lead()
    assert service.skip(lead_id)["skip_reason"] == "manual"
    assert service.restore(lead_id)["status"] == "ready"


def test_unknown_lead_is_404(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    with pytest.raises(service.FlowError) as exc:
        service.restore(12345)
    assert exc.value.status_code == 404


def test_approve_requires_a_draft_and_unapprove_reverses(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    lead_id = seed_lead()
    with pytest.raises(service.FlowError):
        service.approve(lead_id, acknowledge_warnings=False)
    import_store.update_lead(lead_id, status="drafted", subject="S", body="B")
    assert service.approve(lead_id, acknowledge_warnings=False)["status"] == "approved"
    assert service.unapprove(lead_id)["status"] == "drafted"
    with pytest.raises(service.FlowError):
        service.unapprove(lead_id)


def test_approving_a_warned_draft_needs_the_acknowledgement(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    lead_id = seed_lead(status="drafted", subject="S", body="B", review_warnings=["number not in source"])
    with pytest.raises(service.FlowError) as exc:
        service.approve(lead_id, acknowledge_warnings=False)
    assert exc.value.status_code == 409
    assert exc.value.detail["warnings"] == ["number not in source"]
    assert service.approve(lead_id, acknowledge_warnings=True)["status"] == "approved"


def test_a_failed_send_can_be_re_approved_for_retry(monkeypatch, tmp_path):
    use_temp_db(monkeypatch, tmp_path)
    lead_id = seed_lead(status="failed", subject="S", body="B")
    assert service.approve(lead_id, acknowledge_warnings=False)["status"] == "approved"
