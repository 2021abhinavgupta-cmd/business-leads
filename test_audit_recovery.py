"""
Tests for recovering a draft that completed on the server after /api/audit
appeared to fail on the client (added 2026-08-10).

Live-reported: a lead's card showed "Audit failed." with a Retry button, but
a real draft for that same website existed in Saved Drafts. Root cause:
/api/audit is a single request that runs for a couple of minutes, and
nothing in this codebase cancels the handler coroutine if the client's
connection drops mid-request — Starlette does not do this on its own for a
plain response, only if the code explicitly polls request.is_disconnected(),
which app.py never does. A dropped connection (an edge/proxy idle timeout, a
flaky network) therefore looks identical to a real failure to the browser,
while the backend keeps running to completion and saves a real draft anyway.

Every one of app.py's three `{"error": ...}` returns happens BEFORE
db.log_draft is called, so a genuine backend-reported failure never has a
rescuable draft — recovery only matters, and only fires, on the network-level
catch() path.

Unit tests only — no network, no browser, no real DB/API keys.
"""

from datetime import datetime, timedelta

import pytest


def _client(monkeypatch, get_draft_by_website):
    from fastapi.testclient import TestClient
    import app as app_module

    monkeypatch.setattr(app_module.config, "API_KEY", None)
    monkeypatch.setattr(app_module.db, "get_draft_by_website", get_draft_by_website)
    return TestClient(app_module.app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# _is_recoverable — the recency judgment call
# ---------------------------------------------------------------------------

def test_a_draft_from_moments_ago_is_recoverable():
    import app as app_module

    draft = {"timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")}
    assert app_module._is_recoverable(draft) is True


def test_a_draft_from_hours_ago_is_not_recoverable():
    """
    Without this bound, a completely unrelated OLD draft for the same site
    would be handed back whenever the CURRENT audit attempt failed for a
    real reason, making a genuine failure look like a success.
    """
    import app as app_module

    old = datetime.utcnow() - timedelta(hours=3)
    draft = {"timestamp": old.strftime("%Y-%m-%d %H:%M:%S")}
    assert app_module._is_recoverable(draft) is False


def test_a_draft_right_at_the_window_edge_is_still_recoverable():
    import app as app_module

    edge = datetime.utcnow() - timedelta(seconds=app_module._RECOVERABLE_DRAFT_WINDOW_SECONDS - 5)
    draft = {"timestamp": edge.strftime("%Y-%m-%d %H:%M:%S")}
    assert app_module._is_recoverable(draft) is True


def test_no_draft_is_not_recoverable():
    import app as app_module

    assert app_module._is_recoverable(None) is False


def test_a_draft_with_no_timestamp_is_not_recoverable():
    import app as app_module

    assert app_module._is_recoverable({"subject": "x"}) is False


def test_an_unparseable_timestamp_is_not_recoverable():
    """Must fail closed, not raise — a malformed row must not 500 the check."""
    import app as app_module

    assert app_module._is_recoverable({"timestamp": "not a date"}) is False


def test_the_window_reuses_the_audit_cache_ttl():
    """
    Deliberately not an independently-tuned number — it's this codebase's
    existing definition of "still counts as the same audit."
    """
    import app as app_module

    assert app_module._RECOVERABLE_DRAFT_WINDOW_SECONDS == app_module._AUDIT_CACHE_TTL


# ---------------------------------------------------------------------------
# GET /api/audit/recover
# ---------------------------------------------------------------------------

def test_a_fresh_draft_is_returned(monkeypatch):
    draft = {
        "target_email": "owner@example.com",
        "subject": "Quick note",
        "body": "Hi there",
        "image_url": "/screenshots/x.jpg",
        "review_warnings": [],
        "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
    }
    client = _client(monkeypatch, lambda website: draft)
    res = client.get("/api/audit/recover", params={"website": "https://example.com"})

    assert res.status_code == 200
    assert res.json()["draft"]["subject"] == "Quick note"


def test_a_stale_draft_is_withheld(monkeypatch):
    old = datetime.utcnow() - timedelta(days=2)
    draft = {"subject": "Old", "timestamp": old.strftime("%Y-%m-%d %H:%M:%S")}
    client = _client(monkeypatch, lambda website: draft)
    res = client.get("/api/audit/recover", params={"website": "https://example.com"})

    assert res.status_code == 200
    assert res.json()["draft"] is None


def test_no_draft_at_all_returns_none(monkeypatch):
    client = _client(monkeypatch, lambda website: None)
    res = client.get("/api/audit/recover", params={"website": "https://example.com"})

    assert res.status_code == 200
    assert res.json()["draft"] is None


def test_a_db_error_is_a_500_not_a_silent_false_negative(monkeypatch):
    """
    Distinguishing "checked and found nothing" from "couldn't check" matters
    here just as it does everywhere else in this codebase — a 200/None on a
    DB error would look identical to "no draft exists" to the frontend.
    """
    def _explode(website):
        raise RuntimeError("db locked")

    client = _client(monkeypatch, _explode)
    res = client.get("/api/audit/recover", params={"website": "https://example.com"})
    assert res.status_code == 500


# ---------------------------------------------------------------------------
# Wiring — every real error return in /api/audit happens before the draft is
# ever saved, so recovery is only meaningful on the catch() path
# ---------------------------------------------------------------------------

def test_every_error_return_in_audit_lead_precedes_the_draft_save():
    import inspect

    import app as app_module

    source = inspect.getsource(app_module.audit_lead)
    save_point = source.index("db.log_draft")
    error_positions = []
    start = 0
    while True:
        idx = source.find('return {"error"', start)
        if idx == -1:
            break
        error_positions.append(idx)
        start = idx + 1
    assert error_positions, "expected to find the error-return branches"
    assert all(pos < save_point for pos in error_positions), (
        "an error return after the draft save would need recovery handling too"
    )


# ---------------------------------------------------------------------------
# Frontend wiring — source-inspection, matching this codebase's established
# style for asserting on JSX behaviour without a JS test runner
# ---------------------------------------------------------------------------

def _jsx():
    return open("frontend/src/App.jsx", encoding="utf-8").read()


def test_the_catch_block_checks_for_a_recoverable_draft():
    jsx = _jsx()
    catch_block = jsx[jsx.index("} catch (err) {\n      console.error(`Audit failed"):]
    assert "/api/audit/recover" in catch_block[:2000]


def test_a_recovered_draft_is_shown_as_done_not_failed():
    jsx = _jsx()
    assert "recoveredAfterDroppedConnection" in jsx
    assert "updatedLeads[index].auditState = 'done';" in jsx


def test_the_recovery_check_failing_still_falls_back_to_failed():
    """
    The recovery lookup is itself a network call and can fail for the same
    reason the original request did — that must not crash the UI or leave
    the card stuck on a spinner.
    """
    jsx = _jsx()
    catch_block = jsx[jsx.index("} catch (err) {\n      console.error(`Audit failed"):]
    inner_try = catch_block[:catch_block.index("setLeads(prev =>")]
    assert "catch {" in inner_try or "catch (" in inner_try


def test_recovered_score_fields_render_as_na_not_a_crash():
    """
    page_speed_score/seo_score aren't stored on the draft row, so they're
    sent as null — the existing falsy-check ('n/a' fallback) already handles
    that safely; this just confirms the recovered object doesn't invent 0s.
    """
    jsx = _jsx()
    assert "page_speed_score: null" in jsx
    assert "seo_score: null" in jsx
