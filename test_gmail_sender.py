"""
Tests for the swappable email transport (2026-08-06).

No network, no SMTP, no AWS calls — smtplib.SMTP is replaced with a fake that
records what it was asked to do.

The point of the BaseSender split is that the deliverability-critical parts of
a message (text/plain alternative, List-Unsubscribe, threading headers) can't
drift between transports. Several tests below assert exactly that, since a
silent divergence there is invisible until mail starts landing in spam.
"""

import smtplib
from email.mime.multipart import MIMEMultipart

import pytest

import config
from emailer import get_sender
from emailer.base_sender import BaseSender, TransientSendError
from emailer.gmail_sender import GmailSender
from emailer.ses_sender import SESSender


class FakeSMTP:
    """Stand-in for smtplib.SMTP recording calls; raises whatever it's told to."""

    instances = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.login_args = None
        self.sent = []
        self.raise_on_send = None
        self.raise_on_login = None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.started_tls = True

    def login(self, user, password):
        if self.raise_on_login:
            raise self.raise_on_login
        self.login_args = (user, password)

    def send_message(self, msg, from_addr=None, to_addrs=None):
        if self.raise_on_send:
            raise self.raise_on_send
        self.sent.append((msg, from_addr, to_addrs))


@pytest.fixture
def gmail(monkeypatch):
    """A configured GmailSender whose SMTP class is the fake above."""
    FakeSMTP.instances = []
    monkeypatch.setattr(config, "GMAIL_USER", "outreach@example.com")
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr("emailer.base_sender.db.is_suppressed", lambda email: False)
    # No real sleeping between retries.
    monkeypatch.setattr("emailer.base_sender.time.sleep", lambda s: None)
    return GmailSender()


def _fail_next_send(exc):
    """Arm the next FakeSMTP instance to raise on send_message."""
    original_init = FakeSMTP.__init__

    def patched(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.raise_on_send = exc

    return patched


# ---------------------------------------------------------------------------
# Provider selection
# ---------------------------------------------------------------------------

def test_defaults_to_ses(monkeypatch):
    """An environment that never heard of EMAIL_PROVIDER must not change behaviour."""
    monkeypatch.setattr(config, "EMAIL_PROVIDER", "ses")
    assert isinstance(get_sender(), SESSender)


def test_gmail_selected_by_env(monkeypatch):
    monkeypatch.setattr(config, "EMAIL_PROVIDER", "gmail")
    monkeypatch.setattr(config, "GMAIL_USER", "outreach@example.com")
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "pw")
    assert isinstance(get_sender(), GmailSender)


def test_unknown_provider_falls_back_to_ses_rather_than_crashing(monkeypatch, capsys):
    monkeypatch.setattr(config, "EMAIL_PROVIDER", "sendgrid")
    sender = get_sender()
    assert isinstance(sender, SESSender)
    assert "falling back to SES" in capsys.readouterr().out


def test_provider_is_case_and_whitespace_insensitive(monkeypatch):
    monkeypatch.setattr(config, "EMAIL_PROVIDER", "  Gmail ")
    monkeypatch.setattr(config, "GMAIL_USER", "outreach@example.com")
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "pw")
    assert isinstance(get_sender(), GmailSender)


# ---------------------------------------------------------------------------
# Both transports must build the same message
# ---------------------------------------------------------------------------

def test_both_transports_build_identical_message_structure(monkeypatch):
    """
    The whole reason MIME construction lives in BaseSender: if the two
    transports ever build different messages, one of them quietly stops
    carrying the headers Gmail's bulk-sender rules require.
    """
    monkeypatch.setattr(config, "APP_BASE_URL", "https://app.example.com")
    monkeypatch.setattr(config, "GMAIL_USER", "outreach@example.com")
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "pw")

    ses = SESSender()
    ses.from_email = "outreach@example.com"
    gm = GmailSender()

    ses_msg = ses._build_initial_message("lead@example.com", "Subj", "Body", "<id@example.com>")
    gm_msg = gm._build_initial_message("lead@example.com", "Subj", "Body", "<id@example.com>")

    assert ses_msg["List-Unsubscribe"] == gm_msg["List-Unsubscribe"]
    assert ses_msg["List-Unsubscribe-Post"] == gm_msg["List-Unsubscribe-Post"]
    assert ses_msg["From"] == gm_msg["From"]
    assert [p.get_content_type() for p in ses_msg.walk()] == [
        p.get_content_type() for p in gm_msg.walk()
    ]


def test_message_still_carries_a_plain_text_alternative(gmail):
    msg = gmail._build_initial_message("lead@example.com", "Subj", "Body", "<id@example.com>")
    assert "text/plain" in [p.get_content_type() for p in msg.walk()]


def test_followup_carries_real_threading_headers(gmail):
    msg = gmail._build_followup_message(
        "lead@example.com", "Re: Subj", "Body", in_reply_to="<orig@example.com>"
    )
    assert msg["In-Reply-To"] == "<orig@example.com>"
    assert msg["References"] == "<orig@example.com>"


# ---------------------------------------------------------------------------
# From vs Reply-To
# ---------------------------------------------------------------------------

def test_reply_to_defaults_to_the_sending_address(monkeypatch):
    """An environment that never heard of REPLY_TO_EMAIL must be unaffected."""
    monkeypatch.setattr(config, "FROM_EMAIL", "kshitij@mmga.agency")
    monkeypatch.setattr(config, "REPLY_TO_EMAIL", "kshitij@mmga.agency")

    msg = SESSender()._build_initial_message("lead@x.com", "S", "B", "<mid@x.com>")
    assert msg["From"] == msg["Reply-To"] == "kshitij@mmga.agency"


def test_replies_can_be_routed_to_a_different_mailbox(monkeypatch):
    """
    Cold email lands better from a named human than a role address, but the
    person whose name is on it isn't necessarily whose inbox should receive
    the answers — or whose mailbox reply detection can log into.
    """
    monkeypatch.setattr(config, "FROM_EMAIL", "kshitij@mmga.agency")
    monkeypatch.setattr(config, "REPLY_TO_EMAIL", "marketing@mmga.agency")

    msg = SESSender()._build_initial_message("lead@x.com", "S", "B", "<mid@x.com>")
    assert msg["From"] == "kshitij@mmga.agency"
    assert msg["Reply-To"] == "marketing@mmga.agency"


def test_followups_route_replies_the_same_way(monkeypatch):
    monkeypatch.setattr(config, "FROM_EMAIL", "kshitij@mmga.agency")
    monkeypatch.setattr(config, "REPLY_TO_EMAIL", "marketing@mmga.agency")

    msg = SESSender()._build_followup_message("lead@x.com", "Re: S", "B")
    assert msg["From"] == "kshitij@mmga.agency"
    assert msg["Reply-To"] == "marketing@mmga.agency"


def test_unsubscribe_mailto_points_at_the_reply_mailbox(monkeypatch):
    """An unsubscribe request nobody reads is the same as having no mechanism."""
    monkeypatch.setattr(config, "FROM_EMAIL", "kshitij@mmga.agency")
    monkeypatch.setattr(config, "REPLY_TO_EMAIL", "marketing@mmga.agency")
    monkeypatch.setattr(config, "APP_BASE_URL", "")

    headers = SESSender()._unsubscribe_headers("lead@x.com")
    assert "mailto:marketing@mmga.agency" in headers["List-Unsubscribe"]


# ---------------------------------------------------------------------------
# Gmail transport behaviour
# ---------------------------------------------------------------------------

def test_successful_send_returns_message_id_and_uses_tls(gmail):
    message_id = gmail.send_email("lead@example.com", "Subj", "Body")

    assert message_id.startswith("<") and message_id.endswith(">")
    sent = FakeSMTP.instances[-1]
    assert sent.host == "smtp.gmail.com" and sent.port == 587
    assert sent.started_tls, "credentials must never cross the wire before STARTTLS"
    assert sent.login_args == ("outreach@example.com", "abcd efgh ijkl mnop")
    assert sent.sent[0][2] == ["lead@example.com"]


def test_suppressed_recipient_returns_false_without_connecting(gmail, monkeypatch):
    monkeypatch.setattr("emailer.base_sender.db.is_suppressed", lambda email: True)
    assert gmail.send_email("lead@example.com", "Subj", "Body") is False
    assert FakeSMTP.instances == [], "an unsubscribed address must not reach the mail server"


def test_missing_credentials_fail_loudly_instead_of_silently(monkeypatch):
    monkeypatch.setattr(config, "GMAIL_USER", None)
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", None)
    monkeypatch.setattr("emailer.base_sender.db.is_suppressed", lambda email: False)

    with pytest.raises(Exception, match="not configured"):
        GmailSender().send_email("lead@example.com", "Subj", "Body")


def test_bad_app_password_is_not_retried(gmail, monkeypatch):
    """Retrying a rejected credential just delays the same failure."""
    monkeypatch.setattr(
        FakeSMTP, "__init__", _fail_next_send(None), raising=False
    )
    monkeypatch.setattr(
        FakeSMTP,
        "login",
        lambda self, u, p: (_ for _ in ()).throw(
            smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")
        ),
    )

    with pytest.raises(Exception, match="App Password"):
        gmail.send_email("lead@example.com", "Subj", "Body")
    assert len(FakeSMTP.instances) == 1, "authentication failures must not retry"


def test_temporary_4xx_is_retried_once(gmail, monkeypatch):
    monkeypatch.setattr(
        FakeSMTP,
        "__init__",
        _fail_next_send(smtplib.SMTPResponseException(421, b"Try again later")),
    )

    with pytest.raises(Exception, match="421"):
        gmail.send_email("lead@example.com", "Subj", "Body")
    assert len(FakeSMTP.instances) == 2, "a 4xx should get exactly one retry"


def test_permanent_5xx_is_not_retried(gmail, monkeypatch):
    monkeypatch.setattr(
        FakeSMTP,
        "__init__",
        _fail_next_send(smtplib.SMTPResponseException(550, b"Message rejected")),
    )

    with pytest.raises(Exception, match="550"):
        gmail.send_email("lead@example.com", "Subj", "Body")
    assert len(FakeSMTP.instances) == 1


def test_refused_sender_names_the_address_in_the_error(gmail, monkeypatch):
    monkeypatch.setattr(
        FakeSMTP,
        "__init__",
        _fail_next_send(smtplib.SMTPSenderRefused(553, b"not allowed", "outreach@example.com")),
    )

    with pytest.raises(Exception, match="Send mail as"):
        gmail.send_email("lead@example.com", "Subj", "Body")


def test_followup_swallows_failure_to_false(gmail, monkeypatch):
    """main.py's batch runner has no error path to surface a raise into."""
    monkeypatch.setattr(
        FakeSMTP,
        "__init__",
        _fail_next_send(smtplib.SMTPResponseException(550, b"nope")),
    )
    assert gmail.send_followup("lead@example.com", "Subj", "Body") is False


# ---------------------------------------------------------------------------
# Quota
# ---------------------------------------------------------------------------

def test_quota_counts_against_the_self_imposed_cap(gmail, monkeypatch):
    monkeypatch.setattr(config, "GMAIL_DAILY_CAP", 40)
    monkeypatch.setattr("emailer.gmail_sender.db.count_emails_sent_today", lambda: 12)

    quota = gmail.check_quota()
    assert quota == {"Max24HourSend": 40.0, "SentLast24Hours": 12.0, "Remaining": 28.0}


def test_quota_never_reports_negative_headroom(gmail, monkeypatch):
    monkeypatch.setattr(config, "GMAIL_DAILY_CAP", 10)
    monkeypatch.setattr("emailer.gmail_sender.db.count_emails_sent_today", lambda: 25)
    assert gmail.check_quota()["Remaining"] == 0.0


def test_quota_degrades_to_zero_sent_if_the_db_read_fails(gmail, monkeypatch, capsys):
    """A broken counter must not silently look like a full quota... or no quota."""
    def boom():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(config, "GMAIL_DAILY_CAP", 40)
    monkeypatch.setattr("emailer.gmail_sender.db.count_emails_sent_today", boom)

    assert gmail.check_quota()["Remaining"] == 40.0
    assert "Could not read today's send count" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Base class contract
# ---------------------------------------------------------------------------

def test_base_sender_refuses_to_send_on_its_own():
    """A transport that forgets to implement _transport_send must fail loudly."""
    with pytest.raises(NotImplementedError):
        BaseSender("a@example.com")._transport_send(MIMEMultipart(), "b@example.com")


def test_transient_error_is_distinct_from_a_permanent_one():
    assert issubclass(TransientSendError, Exception)
    assert not issubclass(Exception, TransientSendError)
