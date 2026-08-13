"""
Tests for the 3-email follow-up sequence going live (added 2026-08-10).

Prompted by the founder's own framing: email 1 states the problem, email 2
re-offers help, email 3 asks for an explicit yes/no. The code already had
exactly that shape (initial + 2 follow-ups), but two things stood in the
way of it actually working once deployed:

  - scheduler.py — the only thing that ever fires run_followups() — has
    never been deployed as its own Railway service, so in production every
    lead has only ever received the single initial email.
  - nothing stopped a follow-up going out to someone who already replied.
    check_replies() only ever wrote to the local email_replies table;
    run_followups() only ever reads the Google Sheet's Status column, and
    a SheetsStorage.mark_replied() method already existed to bridge that
    gap but was never called from anywhere.

Unit tests only — no real IMAP, no real Sheets, no real deploy.
"""

import imaplib

import pytest

import config


# ---------------------------------------------------------------------------
# The rewritten copy
# ---------------------------------------------------------------------------

def test_stage_one_offers_help_rather_than_just_bumping():
    from emailer.base_sender import BaseSender

    body = BaseSender.__new__(BaseSender).generate_followup("Priya", 1, "Kshitij").lower()
    assert "happy to help" in body


def test_stage_two_asks_an_explicit_yes_or_no():
    """The founder's exact ask: the last email must let the thread close."""
    from emailer.base_sender import BaseSender

    body = BaseSender.__new__(BaseSender).generate_followup("Priya", 2, "Kshitij")
    assert "YES" in body
    assert "NO" in body


def test_the_old_vague_last_one_promise_wording_is_gone():
    from emailer.base_sender import BaseSender

    body = BaseSender.__new__(BaseSender).generate_followup("Priya", 2, "Kshitij").lower()
    assert "last one from me, promise" not in body


def test_followups_still_never_claim_a_mobile_screenshot():
    """The rewrite must not resurrect the false claim fixed 2026-08-09."""
    from emailer.base_sender import BaseSender

    for stage in (1, 2):
        body = BaseSender.__new__(BaseSender).generate_followup("Priya", stage, "Kshitij").lower()
        assert "mobile" not in body


# ---------------------------------------------------------------------------
# Marking a genuine reply in the Sheet — the gap that made "yes or no" unsafe
# ---------------------------------------------------------------------------

class _FakeSheets:
    calls: list = []

    def __init__(self):
        pass

    def find_row_by_email(self, email):
        return {"lead@acme.com": 7}.get(email.lower())

    def mark_replied(self, row):
        _FakeSheets.calls.append(row)


@pytest.fixture(autouse=True)
def _reset_fake_sheets():
    _FakeSheets.calls = []


def test_a_matched_row_is_marked_replied(monkeypatch):
    import emailer.reply_checker as checker

    monkeypatch.setattr("storage.sheets.SheetsStorage", _FakeSheets)
    checker._mark_replied_in_sheet("lead@acme.com")
    assert _FakeSheets.calls == [7]


def test_no_matching_row_does_not_raise(monkeypatch):
    import emailer.reply_checker as checker

    monkeypatch.setattr("storage.sheets.SheetsStorage", _FakeSheets)
    checker._mark_replied_in_sheet("nobody@nowhere.com")  # must not raise
    assert _FakeSheets.calls == []


def test_an_empty_address_is_a_no_op(monkeypatch):
    import emailer.reply_checker as checker

    def _explode():
        raise AssertionError("should not touch Sheets for an empty address")

    monkeypatch.setattr("storage.sheets.SheetsStorage", _explode)
    checker._mark_replied_in_sheet("")


def test_a_sheets_api_failure_does_not_raise(monkeypatch, capsys):
    import emailer.reply_checker as checker

    class _Explodes:
        def find_row_by_email(self, email):
            raise RuntimeError("quota exceeded")

    monkeypatch.setattr("storage.sheets.SheetsStorage", _Explodes)
    checker._mark_replied_in_sheet("lead@acme.com")  # must not raise
    assert "Could not mark" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# End to end through check_replies() — mirrors test_reply_detection.py's
# fixtures so this stays consistent with how that module is already tested.
# ---------------------------------------------------------------------------

def _raw(**headers) -> bytes:
    return ("\r\n".join(f"{k}: {v}" for k, v in headers.items()) + "\r\n\r\n").encode("utf-8")


class _FakeIMAP:
    messages: list = []

    def __init__(self, host, port):
        pass

    def login(self, user, password):
        pass

    def select(self, mailbox, readonly=False):
        return ("OK", [b""])

    def search(self, charset, criteria):
        return ("OK", [b" ".join(str(i).encode() for i in range(1, len(_FakeIMAP.messages) + 1))])

    def fetch(self, number, command):
        return ("OK", [(b"1 (BODY[HEADER]", _FakeIMAP.messages[int(number) - 1]), b")"])

    def logout(self):
        pass


@pytest.fixture
def imap(monkeypatch):
    _FakeIMAP.messages = []
    monkeypatch.setattr(imaplib, "IMAP4_SSL", _FakeIMAP)
    monkeypatch.setattr(config, "IMAP_HOST", "imap.gmail.com")
    monkeypatch.setattr(config, "IMAP_USER", "marketing@mmga.agency")
    monkeypatch.setattr(config, "IMAP_PASSWORD", "abcd efgh ijkl mnop")
    monkeypatch.setattr(config, "REPLY_LOOKBACK_DAYS", 30)


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    from storage import db
    import emailer.reply_checker as checker

    monkeypatch.setattr(db, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.sqlite"))
    monkeypatch.setattr(checker, "db", db)
    return db


def test_a_genuine_reply_calls_the_sheet_marker(imap, temp_db, monkeypatch):
    import emailer.reply_checker as checker

    marked = []
    monkeypatch.setattr(checker, "_mark_replied_in_sheet", lambda email: marked.append(email))
    temp_db.log_email("Acme", "acme.com", "lead@acme.com", "me@x.com", "S", "B", message_id="<sent@x.com>")
    _FakeIMAP.messages = [_raw(**{
        "Message-ID": "<reply@acme.com>",
        "In-Reply-To": "<sent@x.com>",
        "From": "lead@acme.com",
        "Subject": "Re: Quick question",
    })]

    checker.check_replies()
    assert marked == ["lead@acme.com"]


def test_a_bounce_does_not_call_the_sheet_marker(imap, temp_db, monkeypatch):
    """A dead address is not a decision — must not silently close out the lead."""
    import emailer.reply_checker as checker

    marked = []
    monkeypatch.setattr(checker, "_mark_replied_in_sheet", lambda email: marked.append(email))
    temp_db.log_email("Acme", "acme.com", "lead@acme.com", "me@x.com", "S", "B", message_id="<sent@x.com>")
    _FakeIMAP.messages = [_raw(**{
        "Message-ID": "<bounce@mailer-daemon>",
        "In-Reply-To": "<sent@x.com>",
        "From": "Mail Delivery Subsystem <mailer-daemon@x.com>",
        "Subject": "Undelivered Mail Returned to Sender",
        "Content-Type": "multipart/report; report-type=delivery-status",
    })]

    checker.check_replies()
    assert marked == []


def test_an_auto_reply_does_not_call_the_sheet_marker(imap, temp_db, monkeypatch):
    """An out-of-office is not a decision either."""
    import emailer.reply_checker as checker

    marked = []
    monkeypatch.setattr(checker, "_mark_replied_in_sheet", lambda email: marked.append(email))
    temp_db.log_email("Acme", "acme.com", "lead@acme.com", "me@x.com", "S", "B", message_id="<sent@x.com>")
    _FakeIMAP.messages = [_raw(**{
        "Message-ID": "<ooo@acme.com>",
        "In-Reply-To": "<sent@x.com>",
        "From": "lead@acme.com",
        "Subject": "Automatic reply: Out of Office",
        "Auto-Submitted": "auto-replied",
    })]

    checker.check_replies()
    assert marked == []


# ---------------------------------------------------------------------------
# Deployment plumbing — source-inspection, since neither piece is something
# a unit test can exercise for real (a Docker build, a live Railway service)
# ---------------------------------------------------------------------------

def test_the_dockerfile_branches_on_service_role():
    dockerfile = open("Dockerfile", encoding="utf-8").read()
    assert "SERVICE_ROLE" in dockerfile
    assert "scheduler.py" in dockerfile
    assert "uvicorn" in dockerfile


def test_scheduler_checks_replies_before_sending_followups():
    import inspect

    import scheduler

    source = inspect.getsource(scheduler.start_scheduler)
    check_pos = source.index("check_replies_weekdays")
    followup_pos = source.index("run_followups_weekdays")
    assert check_pos < followup_pos, "replies must be checked BEFORE follow-ups are sent"


def test_reply_checking_is_gated_on_imap_being_configured():
    """Matches the existing WARMUP_ENABLED gating pattern in the same file."""
    import inspect

    import scheduler

    source = inspect.getsource(scheduler.start_scheduler)
    assert "config.IMAP_HOST and config.IMAP_USER and config.IMAP_PASSWORD" in source


def test_a_failed_reply_check_cannot_crash_the_scheduler():
    import inspect

    import scheduler

    source = inspect.getsource(scheduler._safe_check_replies)
    assert "except Exception" in source
