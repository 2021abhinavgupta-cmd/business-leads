"""
Tests for db.get_sent_websites_summary() and the /api/sent-websites route
it backs (see App.jsx's "Already sent" badge, added 2026-09-08).

Reported live: searching a niche/city again later shows every lead with
just the plain "Generate AI Audit & Draft" button, even for a business
already emailed in a past session — the frontend's leads array is
per-session localStorage, with nothing connecting a fresh search result
back to a real send already recorded in email_history. This is the
read-only lookup that closes that gap.
"""

from storage import db


def _seed(tmp_path, monkeypatch, rows):
    """rows: list of (company, website, subject)."""
    monkeypatch.setattr(db, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.sqlite"))
    db.init_db()
    for i, (company, website, subject) in enumerate(rows):
        db.log_email(company, website, f"{company}@x.com", "me@x.com", subject, "body", message_id=f"<{i}@x>")
    return db


def test_a_sent_website_appears_in_the_summary(tmp_path, monkeypatch):
    d = _seed(tmp_path, monkeypatch, [("Acme", "https://acme.com", "Your website")])
    summary = d.get_sent_websites_summary()
    assert "acme.com" in summary
    assert summary["acme.com"]["company"] == "Acme"


def test_a_never_emailed_website_is_absent(tmp_path, monkeypatch):
    d = _seed(tmp_path, monkeypatch, [("Acme", "https://acme.com", "Your website")])
    summary = d.get_sent_websites_summary()
    assert "otherbiz.com" not in summary


def test_scheme_and_www_differences_still_match(tmp_path, monkeypatch):
    """
    A lead re-scraped later can come back as a different scheme or with a
    "www." the original send didn't have (or vice versa) — both must
    resolve to the same key, or the badge silently misses real matches.
    """
    d = _seed(tmp_path, monkeypatch, [("Acme", "http://www.acme.com/", "Your website")])
    summary = d.get_sent_websites_summary()
    assert "acme.com" in summary


def test_only_the_most_recent_send_wins_for_a_repeat_website(tmp_path, monkeypatch):
    """
    A follow-up or a re-audit-and-resend produces a second email_history row
    for the same site — the badge should point at the newest one, not an
    arbitrary or the oldest row.
    """
    d = _seed(tmp_path, monkeypatch, [
        ("Acme", "https://acme.com", "First email"),
        ("Acme", "https://acme.com", "Follow up"),
    ])
    summary = d.get_sent_websites_summary()
    assert summary["acme.com"]["subject"] == "Follow up"


def test_a_blank_website_is_never_recorded(tmp_path, monkeypatch):
    """A manually-added lead with no website shouldn't produce a bogus '' key."""
    d = _seed(tmp_path, monkeypatch, [("Acme", "", "Your website")])
    summary = d.get_sent_websites_summary()
    assert "" not in summary


def test_summary_carries_the_row_id_for_the_view_button_to_jump_to(tmp_path, monkeypatch):
    d = _seed(tmp_path, monkeypatch, [("Acme", "https://acme.com", "Your website")])
    summary = d.get_sent_websites_summary()
    assert isinstance(summary["acme.com"]["id"], int)


def test_normalise_website_key_handles_none_and_empty():
    assert db._normalise_website_key("") == ""
    assert db._normalise_website_key(None) == ""


def test_route_is_wired_and_gated_the_same_as_every_other_get(monkeypatch):
    """
    Source-inspection check, same convention this repo uses elsewhere
    (e.g. test_lead_filtering.py's gating assertions) — confirms the route
    goes through db.get_sent_websites_summary rather than a second,
    independent implementation, and is gated by the same auth/rate-limit
    dependencies every other GET route uses.
    """
    import inspect
    import app as app_module

    src = inspect.getsource(app_module.get_sent_websites)
    assert "require_api_key" in src
    assert "rate_limit" in src
    assert "get_sent_websites_summary" in src
