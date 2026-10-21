"""BCC copy of outgoing mail: envelope only, never a header, off unless configured."""
from unittest.mock import MagicMock

import config
from emailer.base_sender import BaseSender
from emailer.ses_sender import SESSender


def test_no_bcc_means_only_the_recipient(monkeypatch):
    monkeypatch.setattr(config, "BCC_EMAIL", "")
    assert BaseSender._envelope_recipients("lead@acme.com") == ["lead@acme.com"]


def test_bcc_is_added_as_an_envelope_recipient(monkeypatch):
    monkeypatch.setattr(config, "BCC_EMAIL", "me@mmga.agency")
    assert BaseSender._envelope_recipients("lead@acme.com") == ["lead@acme.com", "me@mmga.agency"]


def test_bcc_is_not_duplicated_when_it_is_the_recipient(monkeypatch):
    monkeypatch.setattr(config, "BCC_EMAIL", "Me@MMGA.agency")
    assert BaseSender._envelope_recipients("me@mmga.agency") == ["me@mmga.agency"]


def test_ses_sends_to_the_bcc_but_the_message_never_names_it(monkeypatch):
    monkeypatch.setattr(config, "BCC_EMAIL", "me@mmga.agency")
    sender = SESSender.__new__(SESSender)
    sender.client = MagicMock()
    sender.from_email = "kshitij@mmga.agency"
    msg = MagicMock()
    msg.as_string.return_value = "From: a\nTo: lead@acme.com\n\nhi"
    sender._transport_send(msg, "lead@acme.com")
    kwargs = sender.client.send_raw_email.call_args.kwargs
    assert kwargs["Destinations"] == ["lead@acme.com", "me@mmga.agency"]
    assert "me@mmga.agency" not in kwargs["RawMessage"]["Data"]
