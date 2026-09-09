"""
Tests for db.get_drafted_websites_summary() and the /api/drafted-websites
route it backs (see App.jsx's "Draft already made" badge, added 2026-09-09).

Same gap as test_sent_websites.py, one step earlier: a niche/city searched
again later shows every lead with the plain "Generate AI Audit & Draft"
button, even for a business that already has a draft sitting in
email_drafts from an earlier session.
"""

from storage import db


def _seed(tmp_path, monkeypatch, rows):
    """rows: list of (company, website)."""
    monkeypatch.setattr(db, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.sqlite"))
    db.init_db()
    for company, website in rows:
        db.log_draft(company, website, f"{company}@x.com", "Your website", "body")
    return db


def test_a_drafted_website_appears_in_the_summary(tmp_path, monkeypatch):
    d = _seed(tmp_path, monkeypatch, [("Acme", "https://acme.com")])
    summary = d.get_drafted_websites_summary()
    assert "acme.com" in summary
    assert summary["acme.com"]["company"] == "Acme"


def test_a_never_drafted_website_is_absent(tmp_path, monkeypatch):
    d = _seed(tmp_path, monkeypatch, [("Acme", "https://acme.com")])
    summary = d.get_drafted_websites_summary()
    assert "otherbiz.com" not in summary


def test_scheme_and_www_differences_still_match(tmp_path, monkeypatch):
    d = _seed(tmp_path, monkeypatch, [("Acme", "http://www.acme.com/")])
    summary = d.get_drafted_websites_summary()
    assert "acme.com" in summary


def test_only_the_most_recent_draft_wins_for_a_repeat_website(tmp_path, monkeypatch):
    d = _seed(tmp_path, monkeypatch, [
        ("Acme", "https://acme.com"),
        ("Acme Renamed", "https://acme.com"),
    ])
    summary = d.get_drafted_websites_summary()
    assert summary["acme.com"]["company"] == "Acme Renamed"


def test_a_blank_website_is_never_recorded(tmp_path, monkeypatch):
    d = _seed(tmp_path, monkeypatch, [("Acme", "")])
    summary = d.get_drafted_websites_summary()
    assert "" not in summary


def test_summary_carries_the_row_id_for_the_view_button_to_jump_to(tmp_path, monkeypatch):
    d = _seed(tmp_path, monkeypatch, [("Acme", "https://acme.com")])
    summary = d.get_drafted_websites_summary()
    assert isinstance(summary["acme.com"]["id"], int)


def test_route_is_wired_and_gated_the_same_as_every_other_get(monkeypatch):
    """
    Source-inspection check, same convention as test_sent_websites.py's
    equivalent — confirms the route goes through
    db.get_drafted_websites_summary rather than a second, independent
    implementation, and is gated the same as every other GET route.
    """
    import inspect
    import app as app_module

    src = inspect.getsource(app_module.get_drafted_websites)
    assert "require_api_key" in src
    assert "rate_limit" in src
    assert "get_drafted_websites_summary" in src
