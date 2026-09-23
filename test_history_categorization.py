"""
Tests for the sector/niche threading that feeds the History tab's filter
bar (added 2026-09-21, "there should be a filter for how many mails to
agriculture how many to textile and other niches").

Neither sector nor niche was ever persisted to email_history before this —
AuditRequest.sector/sector_detail only fed the draft-generation prompt.
The chain built here: /api/audit stashes them on the draft row it already
creates -> /api/send reads them back off that same draft row (which it
fetches anyway, for the review_warnings gate) -> db.log_email persists them
on the email_history row. No frontend send call site needed to change.
"""

from storage import db


def _use_temp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.sqlite"))


# ---------------------------------------------------------------------------
# storage/db.py round-trip
# ---------------------------------------------------------------------------

def test_a_draft_round_trips_its_sector_and_niche(monkeypatch, tmp_path):
    _use_temp_db(monkeypatch, tmp_path)
    db.log_draft("Acme Farms", "acmefarms.com", "owner@acmefarms.com", "Subject", "Body", sector="agriculture", niche="Organic Farming Supplies")

    draft = db.get_draft_by_website("acmefarms.com")
    assert draft["sector"] == "agriculture"
    assert draft["niche"] == "Organic Farming Supplies"


def test_a_draft_with_no_sector_or_niche_stores_null_not_empty_string(monkeypatch, tmp_path):
    """NULL (not "") so it reads unambiguously as "never categorized", same
    convention as every other optional email_history/email_drafts column."""
    _use_temp_db(monkeypatch, tmp_path)
    db.log_draft("Acme", "acme.com", "owner@acme.com", "Subject", "Body")

    draft = db.get_draft_by_website("acme.com")
    assert draft["sector"] is None
    assert draft["niche"] is None


def test_an_email_history_row_round_trips_its_sector_and_niche(monkeypatch, tmp_path):
    _use_temp_db(monkeypatch, tmp_path)
    db.log_email("Acme", "acme.com", "lead@acme.com", "us@x.com", "S", "B", sector="textile", niche="Fabric Manufacturer")

    row = db.get_email_history()[0]
    assert row["sector"] == "textile"
    assert row["niche"] == "Fabric Manufacturer"


# ---------------------------------------------------------------------------
# db._guess_category / get_email_history's guessed_category (added
# 2026-09-23, "can you go through the whole sent data and fix that") — a
# best-effort label for rows sent before sector/niche existed at all, kept
# strictly separate from real data: it must never appear when a real
# sector/niche is already recorded, and never overwrite that column.
# ---------------------------------------------------------------------------

def test_a_row_with_a_farm_like_company_name_is_guessed_agriculture(monkeypatch, tmp_path):
    _use_temp_db(monkeypatch, tmp_path)
    db.log_email("Acme Farms", "acmefarms.com", "lead@acmefarms.com", "us@x.com", "S", "B")

    row = db.get_email_history()[0]
    assert row["guessed_category"] == "agriculture"
    # The guess must never leak into the real columns.
    assert row["sector"] is None
    assert row["niche"] is None


def test_a_row_with_a_textile_like_website_is_guessed_textile(monkeypatch, tmp_path):
    _use_temp_db(monkeypatch, tmp_path)
    db.log_email("XYZ Enterprises", "xyztextiles.com", "lead@xyztextiles.com", "us@x.com", "S", "B")

    row = db.get_email_history()[0]
    assert row["guessed_category"] == "textile"


def test_a_row_with_no_matching_keywords_is_not_guessed_at_all(monkeypatch, tmp_path):
    _use_temp_db(monkeypatch, tmp_path)
    db.log_email("Sunrise Dental Clinic", "sunrisedental.com", "lead@sunrisedental.com", "us@x.com", "S", "B")

    row = db.get_email_history()[0]
    assert row["guessed_category"] is None


def test_a_row_with_a_real_niche_already_recorded_is_never_guessed_over(monkeypatch, tmp_path):
    """A row that already has real data (even a non-agri/textile niche) is
    fully categorized — guessing on top of it would be pointless at best
    and could only ever add noise, never information."""
    _use_temp_db(monkeypatch, tmp_path)
    db.log_email("Acme Farms Dental", "acmefarms-dental.com", "lead@x.com", "us@x.com", "S", "B", niche="Dentist")

    row = db.get_email_history()[0]
    assert row["guessed_category"] is None
    assert row["niche"] == "Dentist"


def test_a_row_with_a_real_sector_already_recorded_is_never_guessed_over(monkeypatch, tmp_path):
    _use_temp_db(monkeypatch, tmp_path)
    db.log_email("Acme", "acme.com", "lead@x.com", "us@x.com", "S", "B", sector="textile")

    row = db.get_email_history()[0]
    assert row["guessed_category"] is None
    assert row["sector"] == "textile"


def test_the_guess_never_looks_at_subject_or_body_text(monkeypatch, tmp_path):
    """Narrower on purpose — a company's own name/domain is usually a
    direct giveaway, while cold-email prose is long and generic enough
    that a stray word could false-positive."""
    _use_temp_db(monkeypatch, tmp_path)
    db.log_email(
        "Sunrise Dental Clinic", "sunrisedental.com", "lead@x.com", "us@x.com",
        "We help you grow like a farm of new patients",
        "Ever seen a cotton field? Neither have your patients, but here's a metaphor about weaving trust.",
    )

    row = db.get_email_history()[0]
    assert row["guessed_category"] is None


# ---------------------------------------------------------------------------
# app.py wiring: /api/send copies the draft's sector/niche onto the send
# ---------------------------------------------------------------------------

def _send_client_capturing_log_email(monkeypatch, draft):
    from fastapi.testclient import TestClient
    import app as app_module

    monkeypatch.setattr(app_module.config, "API_KEY", None)
    monkeypatch.setattr(app_module.db, "count_emails_sent_today", lambda: 0)
    monkeypatch.setattr(app_module.db, "get_draft_by_website", lambda website: draft)
    monkeypatch.setattr(app_module.ses, "send_email", lambda *a, **k: "<msg-id@example.com>")
    monkeypatch.setattr(app_module.db, "log_cost", lambda *a, **k: None)
    monkeypatch.setattr(app_module.db, "delete_draft_by_website", lambda *a, **k: None)
    monkeypatch.setattr(app_module.sheets, "find_row_by_website", lambda *a, **k: None)

    logged = {}
    monkeypatch.setattr(
        app_module.db, "log_email",
        lambda *a, **k: logged.update(k),
    )
    client = TestClient(app_module.app, raise_server_exceptions=False)
    return client, logged


_SEND_PAYLOAD = {
    "email": "owner@example.com",
    "subject": "Quick note",
    "body": "Hi there",
    "company": "Acme",
    "website": "https://example.com",
}


def test_send_copies_sector_and_niche_from_the_draft(monkeypatch):
    draft = {"timestamp": "2026-09-21 10:00:00", "review_warnings": [], "sector": "agriculture", "niche": "Cotton Seed Dealer"}
    client, logged = _send_client_capturing_log_email(monkeypatch, draft)

    res = client.post("/api/send", json=_SEND_PAYLOAD)

    assert res.status_code == 200, res.text
    assert logged["sector"] == "agriculture"
    assert logged["niche"] == "Cotton Seed Dealer"


def test_send_with_no_matching_draft_logs_blank_sector_and_niche(monkeypatch):
    """/api/send is also called with no draft saved (e.g. a fully manual
    entry) — must not crash, and must fall back to blank rather than
    inventing a category."""
    client, logged = _send_client_capturing_log_email(monkeypatch, None)

    res = client.post("/api/send", json=_SEND_PAYLOAD)

    assert res.status_code == 200, res.text
    assert logged["sector"] == ""
    assert logged["niche"] == ""


def test_send_with_a_draft_that_has_no_sector_or_niche_logs_blank(monkeypatch):
    """A draft saved before this feature existed has no sector/niche keys
    at all — must fall back to blank, not KeyError."""
    draft = {"timestamp": "2026-09-21 10:00:00", "review_warnings": []}
    client, logged = _send_client_capturing_log_email(monkeypatch, draft)

    res = client.post("/api/send", json=_SEND_PAYLOAD)

    assert res.status_code == 200, res.text
    assert logged["sector"] == ""
    assert logged["niche"] == ""
