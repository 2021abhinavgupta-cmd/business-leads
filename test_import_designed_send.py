import pytest

import config
from storage import db
from emailer import base_sender
from emailer.base_sender import BaseSender, TransientSendError

ADDRESS = "MMGA Agency, 12 Example Road, Mumbai 400001, India"
JPEG = b"\xff\xd8\xff\xe0fakejpegbytes"
HTML = (
    "<html><body><p>Hi</p><img src=\"cid:flaw1\"><img src=\"cid:flaw2\">"
    "<div><!--FOOTER--></div></body></html>"
)


class FakeSender(BaseSender):
    provider_name = "fake"
    _RETRY_DELAY_SECONDS = 0

    def __init__(self, failures=0, **kw):
        super().__init__(**kw)
        self.sent = []
        self.failures = failures

    def _transport_send(self, msg, to_email):
        if self.failures:
            self.failures -= 1
            raise TransientSendError("throttled")
        self.sent.append((msg, to_email))


@pytest.fixture(autouse=True)
def _cfg(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.sqlite"))
    monkeypatch.setattr(config, "MAILING_ADDRESS", ADDRESS)
    monkeypatch.setattr(config, "APP_BASE_URL", "https://app.example.com")
    monkeypatch.setattr(config, "REPLY_TO_EMAIL", "reply@example.com")
    monkeypatch.setattr(base_sender.time, "sleep", lambda s: None)


def _sender(**kw):
    return FakeSender(from_email="us@example.com", **kw)


def _build(s=None, html=HTML, text="Plain body  \n"):
    s = s or _sender()
    return s._build_designed_message(
        "a@b.com", "Subj", text, html,
        [("flaw1", JPEG), ("flaw2", JPEG)], "<m@example.com>",
    )


def _texts(msg):
    out = {}
    for part in msg.walk():
        if part.get_content_type() in ("text/plain", "text/html"):
            out[part.get_content_type()] = part.get_payload(decode=True).decode("utf-8")
    return out


def test_with_footer_unchanged_after_refactor(monkeypatch):
    s = _sender()
    expected = f"Hello\n\n{ADDRESS}\nTo stop receiving emails from us, unsubscribe here: https://app.example.com/unsubscribe?email=a%40b.com"
    assert s._with_footer("Hello", "a@b.com") == expected
    assert s._with_footer("Hello \n\n  ", "a@b.com") == expected
    monkeypatch.setattr(config, "APP_BASE_URL", "")
    assert s._with_footer("Hello", "a@b.com") == (
        f"Hello\n\n{ADDRESS}\nTo stop receiving emails from us, reply with the word unsubscribe."
    )
    monkeypatch.setattr(config, "MAILING_ADDRESS", "")
    assert s._with_footer("Hello \n", "a@b.com") == "Hello \n"
    assert s._footer_lines("a@b.com") == []


def test_designed_message_structure_text_before_html_and_images_inline():
    msg = _build()
    assert msg.get_content_type() == "multipart/mixed"
    related = msg.get_payload()[0]
    assert related.get_content_type() == "multipart/related"
    alt = related.get_payload()[0]
    assert alt.get_content_type() == "multipart/alternative"
    assert [p.get_content_type() for p in alt.get_payload()] == ["text/plain", "text/html"]
    imgs = related.get_payload()[1:]
    assert [i["Content-ID"] for i in imgs] == ["<flaw1>", "<flaw2>"]
    for i in imgs:
        assert i.get_content_type() == "image/jpeg"
        assert i["Content-Disposition"].startswith("inline")
        assert i.get_filename()
        assert i.get_payload(decode=True) == JPEG


def test_designed_message_has_unsubscribe_and_reply_to_headers():
    msg = _build()
    assert msg["Reply-To"] == "reply@example.com"
    assert msg["From"] == "us@example.com"
    assert msg["To"] == "a@b.com"
    assert msg["Subject"] == "Subj"
    assert msg["Message-ID"] == "<m@example.com>"
    assert msg["Date"]
    assert "https://app.example.com/unsubscribe?email=a%40b.com" in msg["List-Unsubscribe"]
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_designed_message_has_no_tracking_pixel(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("tracking used")
    monkeypatch.setattr(base_sender, "pixel_html", boom)
    monkeypatch.setattr(base_sender, "tracking_id_for", boom)
    html = _texts(_build())["text/html"]
    assert "/track" not in html and "width=\"1\"" not in html


def test_footer_appears_in_text_and_replaces_the_marker_in_html(monkeypatch):
    monkeypatch.setattr(config, "MAILING_ADDRESS", "A & B <Ltd>, 1 Road")
    t = _texts(_build())
    assert t["text/plain"].startswith("Plain body")
    assert "A & B <Ltd>, 1 Road" in t["text/plain"]
    assert "unsubscribe?email=a%40b.com" in t["text/plain"]
    html = t["text/html"]
    assert "<!--FOOTER-->" not in html
    assert "A &amp; B &lt;Ltd&gt;, 1 Road" in html
    assert "<Ltd>" not in html
    assert 'href="https://app.example.com/unsubscribe?email=a%40b.com"' in html


def test_footer_is_appended_when_marker_missing():
    html = _texts(_build(html="<html><body><p>Hi</p></body></html>"))["text/html"]
    assert ADDRESS in html
    assert html.index(ADDRESS) < html.index("</body>")


def test_marker_removed_without_footer(monkeypatch):
    monkeypatch.setattr(config, "MAILING_ADDRESS", "")
    t = _texts(_build())
    assert "<!--FOOTER-->" not in t["text/html"]
    assert t["text/plain"] == "Plain body  \n"


def test_header_injection_is_blocked():
    s = _sender()
    msg = s._build_designed_message(
        "a@b.com", "Hi\r\nBcc: evil@x.com", "t", HTML, [], "<m@x>")
    assert "\n" not in msg["Subject"] and "\r" not in msg["Subject"]
    assert msg["Bcc"] is None
    with pytest.raises(ValueError):
        s._build_designed_message("a@b.com\r\nBcc: e@x.com", "S", "t", HTML, [], "<m@x>")


def test_send_designed_returns_false_for_suppressed_recipient():
    db.add_suppression("a@b.com")
    s = _sender()
    assert s.send_designed_email("a@b.com", "S", "t", HTML, []) is False
    assert s.sent == []


def test_send_designed_sends_and_returns_message_id():
    s = _sender()
    mid = s.send_designed_email("a@b.com", "S", "t", HTML, [("flaw1", JPEG)])
    assert mid and mid == s.sent[0][0]["Message-ID"]


def test_send_designed_retries_transient_errors_like_send_email():
    s = _sender(failures=1)
    assert s.send_designed_email("a@b.com", "S", "t", HTML, []) 
    assert len(s.sent) == 1
    s2 = _sender(failures=2)
    with pytest.raises(Exception, match="throttled"):
        s2.send_designed_email("a@b.com", "S", "t", HTML, [])
    assert s2.sent == []


def test_send_designed_permanent_error_raises_immediately():
    class Bad(FakeSender):
        def _transport_send(self, msg, to_email):
            raise RuntimeError("rejected")
    with pytest.raises(RuntimeError, match="rejected"):
        Bad(from_email="us@example.com").send_designed_email("a@b.com", "S", "t", HTML, [])
