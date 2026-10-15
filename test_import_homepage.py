from importer import homepage
from security_utils import UnsafeURLError


def _ok_validate(monkeypatch):
    monkeypatch.setattr(homepage, "validate_public_url", lambda url: None)
    monkeypatch.setattr(homepage.trafilatura, "extract", lambda html: "Acme Bank serves families. " * 400)


def test_returns_truncated_clean_text(monkeypatch):
    _ok_validate(monkeypatch)
    text = homepage.fetch_homepage_text("acme.com", get=lambda url: (200, {}, b"<html>x</html>"))
    assert text.startswith("Acme Bank serves families.")
    assert len(text) <= homepage.MAX_TEXT_CHARS


def test_unsafe_url_returns_none_without_fetching(monkeypatch):
    def refuse(url):
        raise UnsafeURLError("private")
    monkeypatch.setattr(homepage, "validate_public_url", refuse)
    called = []
    assert homepage.fetch_homepage_text("10.0.0.1", get=lambda u: called.append(u)) is None
    assert called == []


def test_a_redirect_to_a_private_address_is_refused(monkeypatch):
    def validate(url):
        if "10.0.0.5" in url:
            raise UnsafeURLError("private")
    monkeypatch.setattr(homepage, "validate_public_url", validate)
    calls = []

    def get(url):
        calls.append(url)
        return 302, {"location": "http://10.0.0.5/admin"}, b""

    assert homepage.fetch_homepage_text("acme.com", get=get) is None
    assert calls == ["https://acme.com"]


def test_redirects_are_followed_then_capped(monkeypatch):
    _ok_validate(monkeypatch)
    hops = []

    def get(url):
        hops.append(url)
        return 302, {"location": "/next"}, b""

    assert homepage.fetch_homepage_text("acme.com", get=get) is None
    assert len(hops) == homepage.MAX_REDIRECTS + 1


def test_error_status_and_exceptions_return_none(monkeypatch):
    _ok_validate(monkeypatch)
    assert homepage.fetch_homepage_text("acme.com", get=lambda u: (404, {}, b"nope")) is None

    def boom(url):
        raise OSError("connection reset")

    assert homepage.fetch_homepage_text("acme.com", get=boom) is None
    assert homepage.fetch_homepage_text("", get=boom) is None
