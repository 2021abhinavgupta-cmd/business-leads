"""Dashboard, agriculture, textile and social first-touch emails use the warm-light
theme. The reviewed text stays the source of truth; images and captions stay honest."""
import os

import config
from emailer.base_sender import BaseSender
from importer.email_html import ACCENT, CREAM

BODY = "Hi Sam,\n\nYour contact page hides your phone number.\nIt costs calls.\n\nBest,\nKshitij"


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


def _img(tmp_path, name):
    from PIL import Image
    p = tmp_path / name
    Image.new("RGB", (40, 30), "white").save(p, "JPEG")
    return str(p)


def test_the_html_uses_the_theme_and_the_reviewed_text(monkeypatch):
    monkeypatch.setattr(config, "MAILING_ADDRESS", "")
    plain, html = _parts(_sender()._build_initial_message("a@b.com", "Subj", BODY, "<m@x>"))
    assert CREAM in html and ACCENT in html
    assert "Your contact page hides your phone number." in html
    assert "It costs calls." in html and "<br>" in html     # single newline stays a line break
    assert plain == BODY                                     # text part unchanged


def test_the_text_is_html_escaped(monkeypatch):
    monkeypatch.setattr(config, "MAILING_ADDRESS", "")
    _, html = _parts(_sender()._build_initial_message("a@b.com", "S", "Hi <script>x</script> & co", "<m@x>"))
    assert "<script>" not in html and "&lt;script&gt;" in html and "&amp; co" in html


def test_the_footer_goes_in_the_html_band(monkeypatch):
    monkeypatch.setattr(config, "MAILING_ADDRESS", "MMGA, 12 Road, Mumbai")
    monkeypatch.setattr(config, "APP_BASE_URL", "https://app.example.com")
    _, html = _parts(_sender()._build_initial_message("a@b.com", "S", BODY, "<m@x>"))
    assert "MMGA, 12 Road, Mumbai" in html and "/unsubscribe?email=a%40b.com" in html
    assert "<!--FOOTER-->" not in html


def test_images_and_captions_match_what_is_attached(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MAILING_ADDRESS", "")
    s = _sender()
    msg = s._build_initial_message(
        "a@b.com", "S", BODY, "<m@x>",
        image_path=_img(tmp_path, "d.jpg"), mobile_image_path=_img(tmp_path, "m.jpg"),
        closeup_image_path=None,
    )
    _, html = _parts(msg)
    assert "cid:audit_img" in html and "cid:audit_img_mobile" in html
    assert "cid:audit_img_closeup" not in html
    assert s._IMAGE_CAPTIONS["audit_img"] in html and s._IMAGE_CAPTIONS["audit_img_mobile"] in html
    assert s._IMAGE_CAPTIONS["audit_img_closeup"] not in html


def test_a_missing_file_is_dropped_not_captioned(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MAILING_ADDRESS", "")
    msg = _sender()._build_initial_message(
        "a@b.com", "S", BODY, "<m@x>", mobile_image_path=str(tmp_path / "never_written.jpg"))
    _, html = _parts(msg)
    assert "cid:" not in html and "What we saw" not in html


def test_the_tracking_pixel_is_kept_when_enabled(monkeypatch):
    monkeypatch.setattr(config, "MAILING_ADDRESS", "")
    import emailer.base_sender as bs
    monkeypatch.setattr(bs, "pixel_html", lambda tid: '<img src="https://x/o/abc.gif" width="1" height="1">')
    plain, html = _parts(_sender()._build_initial_message("a@b.com", "S", BODY, "<m@x>"))
    assert "/o/abc.gif" in html and "/o/abc.gif" not in plain


def test_followups_stay_plain_threaded_replies():
    # A follow-up should read as a personal reply, not a designed mailer.
    msg = _sender()._build_followup_message("a@b.com", "Re: S", "Just checking in.", "<orig@x>")
    _, html = _parts(msg)
    assert CREAM not in html
