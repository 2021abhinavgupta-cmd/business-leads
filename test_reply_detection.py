"""
Tests for IMAP reply detection (2026-08-06).

No network — imaplib.IMAP4_SSL is replaced with a fake mailbox built from
real header shapes.

A reply is the only engagement signal here that's supposed to be exact, so
the failure that matters most is a *false* one: counting an out-of-office or
a bounce notification as somebody writing back would quietly corrupt the one
number worth trusting. Most of these tests are about that boundary.
"""

import imaplib

import pytest

import config
from emailer.reply_checker import (
    _extract_message_ids,
    check_replies,
    is_auto_reply,
    is_bounce,
)


def _raw(**headers) -> bytes:
    return ("\r\n".join(f"{k}: {v}" for k, v in headers.items()) + "\r\n\r\n").encode("utf-8")


class FakeIMAP:
    """Stand-in for imaplib.IMAP4_SSL holding a fixed list of raw header blocks."""

    messages: list = []
    instances: list = []

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.logged_in_as = None
        self.selected = None
        self.readonly = None
        self.fetch_commands = []
        self.logged_out = False
        FakeIMAP.instances.append(self)

    def login(self, user, password):
        self.logged_in_as = (user, password)

    def select(self, mailbox, readonly=False):
        self.selected = mailbox
        self.readonly = readonly
        return ("OK", [b""])

    def search(self, charset, criteria):
        self.search_criteria = criteria
        return ("OK", [b" ".join(str(i).encode() for i in range(1, len(FakeIMAP.messages) + 1))])

    def fetch(self, number, command):
        self.fetch_commands.append(command)
        raw = FakeIMAP.messages[int(number) - 1]
        return ("OK", [(b"1 (BODY[HEADER]", raw), b")"])

    def logout(self):
        self.logged_out = True


@pytest.fixture
def imap(monkeypatch):
    FakeIMAP.messages = []
    FakeIMAP.instances = []
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeIMAP)
    monkeypatch.setattr(config, "IMAP_HOST", "imap.gmail.com")
    monkeypatch.setattr(config, "IMAP_USER", "marketing@mmga.agency")
    monkeypatch.setattr(config, "IMAP_PASSWORD", "abcd efgh ijkl mnop")
    monkeypatch.setattr(config, "REPLY_LOOKBACK_DAYS", 30)
    return FakeIMAP


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    from storage import db
    import emailer.reply_checker as checker

    monkeypatch.setattr(db, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.sqlite"))
    monkeypatch.setattr(checker, "db", db)
    return db


# ---------------------------------------------------------------------------
# Header parsing
# ---------------------------------------------------------------------------

def test_extracts_every_id_from_a_references_chain():
    ids = _extract_message_ids("<a@x.com> <b@x.com>\r\n <c@x.com>")
    assert ids == ["<a@x.com>", "<b@x.com>", "<c@x.com>"]


def test_missing_header_yields_no_ids():
    assert _extract_message_ids("") == []
    assert _extract_message_ids(None) == []


# ---------------------------------------------------------------------------
# Auto-replies must never count as engagement
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("headers", [
    {"auto-submitted": "auto-replied"},
    {"x-autoreply": "yes"},
    {"x-autorespond": "true"},
    {"precedence": "bulk"},
    {"subject": "Automatic reply: Quick question about your website"},
    {"subject": "Out of Office: your email"},
    {"subject": "Re: Automatic reply: your email"},
    {"subject": "Auto: away until Monday"},
])
def test_machine_generated_replies_are_flagged(headers):
    assert is_auto_reply(headers) is True


@pytest.mark.parametrize("headers", [
    {"subject": "Re: Quick question about your website"},
    {"subject": "Re: your email — interested, can we talk Thursday?"},
    {"auto-submitted": "no"},
    {},
])
def test_genuine_replies_are_not_flagged(headers):
    assert is_auto_reply(headers) is False


def test_out_of_office_is_not_counted_as_a_reply(imap, temp_db):
    """An autoresponder proves the address is live, not that anyone read anything."""
    temp_db.log_email("Acme", "acme.com", "lead@acme.com", "me@x.com", "S", "B", message_id="<sent@x.com>")
    FakeIMAP.messages = [_raw(**{
        "Message-ID": "<ooo@acme.com>",
        "In-Reply-To": "<sent@x.com>",
        "From": "Lead <lead@acme.com>",
        "Subject": "Automatic reply: Quick question",
        "Auto-Submitted": "auto-replied",
    })]

    summary = check_replies()
    assert summary["auto_replies"] == 1
    assert summary["replies"] == 0
    assert temp_db.get_email_history()[0]["replied"] is False
    assert temp_db.get_email_history()[0]["auto_replied"] is True


# ---------------------------------------------------------------------------
# Bounces
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("headers", [
    {"from": "Mail Delivery Subsystem <mailer-daemon@googlemail.com>"},
    {"from": "postmaster@example.com"},
    {"subject": "Undeliverable: Quick question about your website"},
    {"subject": "Delivery Status Notification (Failure)"},
    {"return-path": "<>", "content-type": "multipart/report; report-type=delivery-status"},
])
def test_delivery_failures_are_flagged(headers):
    assert is_bounce(headers) is True


def test_a_bounce_is_not_counted_as_a_reply(imap, temp_db):
    temp_db.log_email("Acme", "acme.com", "lead@acme.com", "me@x.com", "S", "B", message_id="<sent@x.com>")
    FakeIMAP.messages = [_raw(**{
        "Message-ID": "<bounce@google.com>",
        "In-Reply-To": "<sent@x.com>",
        "From": "Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
        "Subject": "Delivery Status Notification (Failure)",
    })]

    summary = check_replies()
    assert summary["bounces"] == 1
    assert summary["replies"] == 0

    row = temp_db.get_email_history()[0]
    assert row["bounced"] is True and row["replied"] is False


def test_a_bounce_does_not_silently_suppress_the_address(imap, temp_db):
    """
    Identifying which address failed needs the delivery-status body, which
    this deliberately doesn't fetch. Suppressing on a guess would
    permanently block a real lead, so bounces are reported, not acted on.
    """
    temp_db.log_email("Acme", "acme.com", "lead@acme.com", "me@x.com", "S", "B", message_id="<sent@x.com>")
    FakeIMAP.messages = [_raw(**{
        "Message-ID": "<bounce@google.com>",
        "In-Reply-To": "<sent@x.com>",
        "From": "mailer-daemon@googlemail.com",
        "Subject": "Undeliverable",
    })]

    check_replies()
    assert temp_db.is_suppressed("lead@acme.com") is False


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def test_reply_matched_by_threading_header(imap, temp_db):
    temp_db.log_email("Acme", "acme.com", "lead@acme.com", "me@x.com", "S", "B", message_id="<sent@x.com>")
    FakeIMAP.messages = [_raw(**{
        "Message-ID": "<reply@acme.com>",
        "In-Reply-To": "<sent@x.com>",
        "From": "Someone Else <assistant@acme.com>",
        "Subject": "Re: Quick question",
    })]

    assert check_replies()["replies"] == 1
    assert temp_db.get_email_history()[0]["replied"] is True


def test_reply_matched_by_sender_when_threading_headers_are_missing(imap, temp_db):
    """
    Plenty of clients drop In-Reply-To. Losing a real reply is worse than the
    small chance of crediting it to the wrong send.
    """
    temp_db.log_email("Acme", "acme.com", "lead@acme.com", "me@x.com", "S", "B", message_id="<sent@x.com>")
    FakeIMAP.messages = [_raw(**{
        "Message-ID": "<reply@acme.com>",
        "From": "Lead <LEAD@ACME.COM>",
        "Subject": "your email",
    })]

    assert check_replies()["replies"] == 1
    assert temp_db.get_email_history()[0]["replied"] is True


def test_reply_matched_via_references_when_in_reply_to_is_absent(imap, temp_db):
    temp_db.log_email("Acme", "acme.com", "lead@acme.com", "me@x.com", "S", "B", message_id="<sent@x.com>")
    FakeIMAP.messages = [_raw(**{
        "Message-ID": "<reply@acme.com>",
        "References": "<older@x.com> <sent@x.com>",
        "From": "stranger@elsewhere.com",
        "Subject": "Re: Quick question",
    })]

    assert check_replies()["replies"] == 1


def test_unrelated_mail_is_ignored(imap, temp_db):
    temp_db.log_email("Acme", "acme.com", "lead@acme.com", "me@x.com", "S", "B", message_id="<sent@x.com>")
    FakeIMAP.messages = [_raw(**{
        "Message-ID": "<newsletter@somewhere.com>",
        "From": "news@somewhere.com",
        "Subject": "Your weekly digest",
    })]

    summary = check_replies()
    assert summary["unmatched"] == 1 and summary["replies"] == 0
    assert temp_db.get_replies() == []


def test_a_real_reply_outranks_an_earlier_auto_reply(imap, temp_db):
    """An out-of-office followed by a genuine answer is a genuine answer."""
    temp_db.log_email("Acme", "acme.com", "lead@acme.com", "me@x.com", "S", "B", message_id="<sent@x.com>")
    FakeIMAP.messages = [
        _raw(**{"Message-ID": "<ooo@acme.com>", "In-Reply-To": "<sent@x.com>",
                "From": "lead@acme.com", "Subject": "Out of office"}),
        _raw(**{"Message-ID": "<real@acme.com>", "In-Reply-To": "<sent@x.com>",
                "From": "lead@acme.com", "Subject": "Re: Quick question"}),
    ]

    check_replies()
    assert temp_db.get_email_history()[0]["replied"] is True


# ---------------------------------------------------------------------------
# Rescanning
# ---------------------------------------------------------------------------

def test_rescanning_the_same_mailbox_does_not_double_count(imap, temp_db):
    """
    The window is 30 days by default, so every scan re-reads the same
    replies. Without idempotence the reply count would climb on its own.
    """
    temp_db.log_email("Acme", "acme.com", "lead@acme.com", "me@x.com", "S", "B", message_id="<sent@x.com>")
    FakeIMAP.messages = [_raw(**{
        "Message-ID": "<reply@acme.com>", "In-Reply-To": "<sent@x.com>",
        "From": "lead@acme.com", "Subject": "Re: Quick question",
    })]

    check_replies()
    check_replies()
    check_replies()

    assert len(temp_db.get_replies()) == 1


def test_replies_without_a_message_id_stay_idempotent(imap, temp_db):
    """Empty Message-IDs must not all collide on one primary key."""
    temp_db.log_email("Acme", "acme.com", "a@acme.com", "me@x.com", "S", "B", message_id="<one@x.com>")
    temp_db.log_email("Beta", "beta.com", "b@beta.com", "me@x.com", "S", "B", message_id="<two@x.com>")
    FakeIMAP.messages = [
        _raw(**{"In-Reply-To": "<one@x.com>", "From": "a@acme.com", "Subject": "Re: one"}),
        _raw(**{"In-Reply-To": "<two@x.com>", "From": "b@beta.com", "Subject": "Re: two"}),
    ]

    check_replies()
    check_replies()

    assert len(temp_db.get_replies()) == 2, "two distinct replies, scanned twice"


# ---------------------------------------------------------------------------
# Connection behaviour
# ---------------------------------------------------------------------------

def test_scan_is_read_only_and_never_marks_mail_as_read(imap, temp_db):
    """
    This mailbox is a human's inbox. Marking their mail read as a side effect
    of a background poll would be its own bug.
    """
    temp_db.log_email("Acme", "acme.com", "lead@acme.com", "me@x.com", "S", "B", message_id="<sent@x.com>")
    FakeIMAP.messages = [_raw(**{"In-Reply-To": "<sent@x.com>", "From": "lead@acme.com", "Subject": "Re: hi"})]

    check_replies()

    connection = FakeIMAP.instances[-1]
    assert connection.readonly is True
    assert all("BODY.PEEK" in cmd for cmd in connection.fetch_commands)
    assert not any("BODY[" in cmd.replace("BODY.PEEK[", "") for cmd in connection.fetch_commands)


def test_only_headers_are_fetched_never_the_body(imap, temp_db):
    FakeIMAP.messages = [_raw(**{"From": "x@y.com", "Subject": "hi"})]
    check_replies()
    assert all("HEADER.FIELDS" in cmd for cmd in FakeIMAP.instances[-1].fetch_commands)


def test_connection_is_closed_even_when_scanning_explodes(imap, temp_db, monkeypatch):
    monkeypatch.setattr(FakeIMAP, "search", lambda self, c, q: ("NO", [b""]))

    with pytest.raises(Exception, match="IMAP search failed"):
        check_replies()

    assert FakeIMAP.instances[-1].logged_out is True


def test_one_unparseable_message_does_not_abort_the_scan(imap, temp_db, monkeypatch):
    temp_db.log_email("Acme", "acme.com", "lead@acme.com", "me@x.com", "S", "B", message_id="<sent@x.com>")
    FakeIMAP.messages = [b"garbage", _raw(**{
        "Message-ID": "<reply@acme.com>", "In-Reply-To": "<sent@x.com>",
        "From": "lead@acme.com", "Subject": "Re: Quick question",
    })]

    original_fetch = FakeIMAP.fetch

    def flaky_fetch(self, number, command):
        if int(number) == 1:
            raise RuntimeError("connection reset mid-fetch")
        return original_fetch(self, number, command)

    monkeypatch.setattr(FakeIMAP, "fetch", flaky_fetch)

    assert check_replies()["replies"] == 1, "a bad message must not cost us the good ones"


def test_unconfigured_imap_explains_itself(monkeypatch):
    monkeypatch.setattr(config, "IMAP_USER", None)
    monkeypatch.setattr(config, "IMAP_PASSWORD", None)

    with pytest.raises(Exception, match="App Password"):
        check_replies()


def test_search_window_uses_the_configured_lookback(imap, temp_db, monkeypatch):
    monkeypatch.setattr(config, "REPLY_LOOKBACK_DAYS", 7)
    check_replies()
    assert "SINCE" in FakeIMAP.instances[-1].search_criteria
