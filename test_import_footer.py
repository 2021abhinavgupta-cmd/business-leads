import config
from emailer.base_sender import BaseSender

ADDRESS = "MMGA Agency, 12 Example Road, Mumbai 400001, India"


def _sender():
    return BaseSender(from_email="us@example.com")


def _parts(msg):
    plain = html = ""
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            plain = part.get_payload(decode=True).decode("utf-8")
        elif part.get_content_type() == "text/html":
            html = part.get_payload(decode=True).decode("utf-8")
    return plain, html


def test_unset_address_leaves_bodies_byte_identical(monkeypatch):
    monkeypatch.setattr(config, "MAILING_ADDRESS", "")
    s = _sender()
    assert s._with_footer("Hello there", "a@b.com") == "Hello there"
    plain, html = _parts(s._build_initial_message("a@b.com", "S", "Hello there", "<m@x>"))
    assert plain == "Hello there"
    assert "MMGA Agency" not in html


def test_initial_email_carries_address_and_optout_link(monkeypatch):
    monkeypatch.setattr(config, "MAILING_ADDRESS", ADDRESS)
    monkeypatch.setattr(config, "APP_BASE_URL", "https://app.example.com")
    plain, html = _parts(_sender()._build_initial_message("a@b.com", "S", "Hello there", "<m@x>"))
    assert plain.startswith("Hello there")
    assert ADDRESS in plain and ADDRESS in html
    assert "https://app.example.com/unsubscribe?email=a%40b.com" in plain


def test_without_a_base_url_the_optout_is_a_reply_instruction(monkeypatch):
    monkeypatch.setattr(config, "MAILING_ADDRESS", ADDRESS)
    monkeypatch.setattr(config, "APP_BASE_URL", "")
    footer = _sender()._with_footer("Hello", "a@b.com")
    assert "reply with the word unsubscribe" in footer


def test_followups_carry_the_footer_too(monkeypatch):
    monkeypatch.setattr(config, "MAILING_ADDRESS", ADDRESS)
    monkeypatch.setattr(config, "APP_BASE_URL", "https://app.example.com")
    plain, _ = _parts(_sender()._build_followup_message("a@b.com", "Re: S", "Nudge", "<o@x>"))
    assert ADDRESS in plain


def test_footer_wording_has_no_dashes(monkeypatch):
    monkeypatch.setattr(config, "MAILING_ADDRESS", "MMGA Agency, 12 Example Road, Mumbai")
    monkeypatch.setattr(config, "APP_BASE_URL", "https://app.example.com")
    footer = _sender()._with_footer("", "a@b.com")
    assert not any(d in footer for d in ("-", "—", "–"))
