"""
Tests for email open tracking (2026-08-06).

No network. Database tests run against a throwaway SQLite file.

The recurring theme here is that a tracking pixel fails in ways nobody sees:
a pixel that never got embedded, a hit that never got logged, an endpoint
that 500s into a broken-image icon in someone's inbox. Each of those looks
exactly like "nobody opened it" from the dashboard, so they're asserted
directly rather than inferred.
"""

import sqlite3

import pytest

import config
from emailer.tracking import (
    TRANSPARENT_GIF,
    hash_ip,
    looks_automated,
    pixel_html,
    pixel_url,
    tracking_id_for,
)


@pytest.fixture
def tracking_on(monkeypatch):
    monkeypatch.setattr(config, "EMAIL_OPEN_TRACKING", True)
    monkeypatch.setattr(config, "APP_BASE_URL", "https://app.example.com")


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    """Point storage.db at a scratch file so tests never touch real send history."""
    from storage import db

    monkeypatch.setattr(db, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.sqlite"))
    return db


# ---------------------------------------------------------------------------
# Tracking IDs
# ---------------------------------------------------------------------------

def test_tracking_id_is_stable_for_the_same_message():
    """
    The sender derives the ID when building the pixel; app.py derives it
    again later from the stored Message-ID. If those two ever disagree,
    every open silently fails to match a message.
    """
    assert tracking_id_for("<abc@mmga.agency>") == tracking_id_for("<abc@mmga.agency>")


def test_different_messages_get_different_ids():
    assert tracking_id_for("<a@x.com>") != tracking_id_for("<b@x.com>")


def test_tracking_id_matches_the_shape_the_endpoint_accepts():
    """The route filters on exactly this shape, so the two must agree."""
    tid = tracking_id_for("<abc@mmga.agency>")
    assert len(tid) == 18
    assert all(c in "0123456789abcdef" for c in tid)


def test_no_message_id_yields_no_tracking_id():
    assert tracking_id_for("") == ""


def test_ip_hash_is_not_reversible_to_the_address():
    hashed = hash_ip("203.0.113.7")
    assert "203" not in hashed and "113" not in hashed
    assert hashed == hash_ip("203.0.113.7"), "must be stable enough to spot repeat fetches"
    assert hash_ip("") == ""


# ---------------------------------------------------------------------------
# Pixel HTML
# ---------------------------------------------------------------------------

def test_pixel_is_empty_when_tracking_is_off(monkeypatch):
    monkeypatch.setattr(config, "EMAIL_OPEN_TRACKING", False)
    monkeypatch.setattr(config, "APP_BASE_URL", "https://app.example.com")
    assert pixel_html(tracking_id_for("<a@x.com>")) == ""


def test_pixel_is_empty_without_app_base_url(monkeypatch):
    """A relative URL inside an email resolves against nothing."""
    monkeypatch.setattr(config, "EMAIL_OPEN_TRACKING", True)
    monkeypatch.setattr(config, "APP_BASE_URL", "")
    assert pixel_html(tracking_id_for("<a@x.com>")) == ""


def test_pixel_points_at_the_absolute_tracking_url(tracking_on):
    tid = tracking_id_for("<a@x.com>")
    html = pixel_html(tid)
    assert f'src="https://app.example.com/o/{tid}.gif"' in html


def test_pixel_does_not_use_display_none(tracking_on):
    """Hiding an image is its own spam heuristic; a real 1x1 is already invisible."""
    html = pixel_html(tracking_id_for("<a@x.com>"))
    assert "display" not in html.lower()
    assert 'width="1"' in html and 'height="1"' in html


def test_the_gif_is_a_real_gif():
    assert TRANSPARENT_GIF.startswith(b"GIF89a")


# ---------------------------------------------------------------------------
# Bot detection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("agent", [
    "curl/8.4.0",
    "python-requests/2.31.0",
    "Mozilla/5.0 (compatible; Googlebot/2.1)",
    "Barracuda Email Security",
    "",
])
def test_obvious_machines_are_flagged(agent):
    assert looks_automated(agent) is True


@pytest.mark.parametrize("agent", [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile/15E148",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Gecko/20100101 Firefox/121.0",
])
def test_real_clients_are_not_flagged(agent):
    assert looks_automated(agent) is False


def test_gmail_image_proxy_counts_as_a_real_open():
    """
    Gmail fetches through its proxy when a user opens the message. Treating
    it as automated would throw away most of the signal — the recipients here
    are overwhelmingly on Gmail.
    """
    assert looks_automated(
        "Mozilla/5.0 (Windows NT 5.1; rv:11.0) Gecko Firefox/11.0 (via ggpht.com GoogleImageProxy)"
    ) is False


# ---------------------------------------------------------------------------
# Pixel embedding in real messages
# ---------------------------------------------------------------------------

def _html_parts(msg):
    return [
        p.get_payload(decode=True).decode("utf-8")
        for p in msg.walk()
        if p.get_content_type() == "text/html"
    ]


def _plain_parts(msg):
    return [
        p.get_payload(decode=True).decode("utf-8")
        for p in msg.walk()
        if p.get_content_type() == "text/plain"
    ]


def test_sent_message_embeds_the_pixel_when_enabled(tracking_on):
    from emailer.ses_sender import SESSender

    sender = SESSender()
    msg = sender._build_initial_message("lead@example.com", "Subj", "Body", "<mid@mmga.agency>")

    expected = tracking_id_for("<mid@mmga.agency>")
    assert any(f"/o/{expected}.gif" in part for part in _html_parts(msg))


def test_sent_message_has_no_pixel_when_disabled(monkeypatch):
    from emailer.ses_sender import SESSender

    monkeypatch.setattr(config, "EMAIL_OPEN_TRACKING", False)
    sender = SESSender()
    msg = sender._build_initial_message("lead@example.com", "Subj", "Body", "<mid@mmga.agency>")

    assert not any("/o/" in part for part in _html_parts(msg))


def test_plain_text_part_never_shows_the_tracking_url(tracking_on):
    """A text/plain part can't load an image — a URL there is just visible to the reader."""
    from emailer.ses_sender import SESSender

    msg = SESSender()._build_initial_message("lead@example.com", "Subj", "Body", "<mid@x.com>")
    assert all("/o/" not in part for part in _plain_parts(msg))


def test_followup_is_tracked_with_its_own_id(tracking_on):
    from emailer.ses_sender import SESSender

    msg = SESSender()._build_followup_message("lead@example.com", "Re: Subj", "Body")
    expected = tracking_id_for(msg["Message-ID"])
    assert any(f"/o/{expected}.gif" in part for part in _html_parts(msg)), (
        "the follow-up's pixel must match its own Message-ID, not the original's"
    )


def test_both_transports_embed_the_same_pixel(tracking_on, monkeypatch):
    from emailer.gmail_sender import GmailSender
    from emailer.ses_sender import SESSender

    monkeypatch.setattr(config, "GMAIL_USER", "outreach@example.com")
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "pw")

    ses_msg = SESSender()._build_initial_message("l@x.com", "S", "B", "<mid@x.com>")
    gm_msg = GmailSender()._build_initial_message("l@x.com", "S", "B", "<mid@x.com>")

    tid = tracking_id_for("<mid@x.com>")
    assert any(f"/o/{tid}.gif" in p for p in _html_parts(ses_msg))
    assert any(f"/o/{tid}.gif" in p for p in _html_parts(gm_msg))


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def test_opens_aggregate_per_message(temp_db):
    tid = tracking_id_for("<a@x.com>")
    temp_db.log_email_open(tid, "Mozilla/5.0 Chrome", "hash1", is_automated=False)
    temp_db.log_email_open(tid, "Mozilla/5.0 Chrome", "hash1", is_automated=False)

    stats = temp_db.get_open_summary()[tid]
    assert stats["open_count"] == 2
    assert stats["automated_count"] == 0
    assert stats["first_opened_at"] and stats["last_opened_at"]


def test_scanner_hits_are_counted_separately_not_as_opens(temp_db):
    """
    A message whose only hits came from a security scanner has not been read.
    Folding those into open_count would report a lead as engaged when nobody
    ever saw the mail.
    """
    tid = tracking_id_for("<a@x.com>")
    temp_db.log_email_open(tid, "Barracuda", "h", is_automated=True)

    stats = temp_db.get_open_summary()[tid]
    assert stats["open_count"] == 0
    assert stats["automated_count"] == 1
    assert stats["first_opened_at"] is None


def test_history_reports_opens_against_the_right_message(temp_db):
    temp_db.log_email("Acme", "acme.com", "a@acme.com", "me@x.com", "S", "B", message_id="<one@x.com>")
    temp_db.log_email("Beta", "beta.com", "b@beta.com", "me@x.com", "S", "B", message_id="<two@x.com>")
    temp_db.log_email_open(tracking_id_for("<one@x.com>"), "Chrome", "h", is_automated=False)

    by_company = {row["company"]: row for row in temp_db.get_email_history()}
    assert by_company["Acme"]["open_count"] == 1
    assert by_company["Beta"]["open_count"] == 0


def test_history_rows_predating_tracking_do_not_crash(temp_db):
    """Older rows have no message_id at all; they must read as zero, not raise."""
    temp_db.log_email("Old", "old.com", "o@old.com", "me@x.com", "S", "B")
    row = temp_db.get_email_history()[0]
    assert row["open_count"] == 0
    assert row["first_opened_at"] is None


def test_open_log_survives_a_fresh_connection(temp_db):
    """Every db function opens its own connection — the write must actually commit."""
    tid = tracking_id_for("<a@x.com>")
    temp_db.log_email_open(tid, "Chrome", "h")

    conn = sqlite3.connect(temp_db.DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM email_opens").fetchone()[0]
    conn.close()
    assert count == 1


def test_long_user_agent_is_truncated_not_rejected(temp_db):
    tid = tracking_id_for("<a@x.com>")
    temp_db.log_email_open(tid, "X" * 5000, "h")
    assert temp_db.get_open_summary()[tid]["open_count"] == 1


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch, temp_db):
    from fastapi.testclient import TestClient
    import app as app_module

    monkeypatch.setattr(app_module.db, "DB_DIR", temp_db.DB_DIR)
    monkeypatch.setattr(app_module.db, "DB_PATH", temp_db.DB_PATH)
    return TestClient(app_module.app)


def test_pixel_request_returns_a_gif_and_logs_the_open(client, temp_db):
    tid = tracking_id_for("<a@x.com>")
    response = client.get(f"/o/{tid}.gif", headers={"user-agent": "Mozilla/5.0 Chrome"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/gif"
    assert response.content == TRANSPARENT_GIF
    assert temp_db.get_open_summary()[tid]["open_count"] == 1


def test_pixel_response_is_uncacheable(client):
    """A cached pixel means the second open never reaches us."""
    response = client.get(f"/o/{tracking_id_for('<a@x.com>')}.gif")
    assert "no-store" in response.headers["cache-control"]


def test_pixel_needs_no_api_key(client):
    """The caller is a mail client. It has no key and no way to retry a 401."""
    response = client.get(f"/o/{tracking_id_for('<a@x.com>')}.gif")
    assert response.status_code == 200


def test_garbage_tracking_ids_are_not_stored(client, temp_db):
    """Scanners probe every path; they must not be able to fill the table."""
    for bad in ["notarealid", "../../etc/passwd", "z" * 18, ""]:
        client.get(f"/o/{bad}.gif")
    assert temp_db.get_open_summary() == {}


def test_a_logging_failure_still_returns_the_image(client, monkeypatch):
    """
    A 500 here renders as a broken-image icon inside the recipient's inbox —
    visibly odd, and a giveaway that the mail is tracked.
    """
    import app as app_module

    def boom(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(app_module.db, "log_email_open", boom)

    response = client.get(f"/o/{tracking_id_for('<a@x.com>')}.gif")
    assert response.status_code == 200
    assert response.content == TRANSPARENT_GIF


def test_history_endpoint_reports_whether_tracking_was_even_on(client, monkeypatch):
    """
    Without this flag the dashboard can't tell "nobody opened it" from
    "opens were never being measured" — both are open_count 0.
    """
    import app as app_module

    # Auth is a separate concern; keep this test focused on the flag.
    monkeypatch.setattr(app_module.config, "API_KEY", None)

    monkeypatch.setattr(config, "EMAIL_OPEN_TRACKING", False)
    body = client.get("/api/history").json()
    assert body["tracking_enabled"] is False

    monkeypatch.setattr(config, "EMAIL_OPEN_TRACKING", True)
    monkeypatch.setattr(config, "APP_BASE_URL", "https://app.example.com")
    assert client.get("/api/history").json()["tracking_enabled"] is True
