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


# ---- _http_get over a mock transport --------------------------------------

import httpx


def test_http_get_caps_body_and_sends_identity_encoding_and_agent():
    seen = {}

    def handler(request):
        seen["headers"] = request.headers
        return httpx.Response(200, stream=httpx.ByteStream(b"x" * (homepage.MAX_BODY_BYTES + 5000)))

    status, _, body = homepage._http_get("https://acme.com", transport=httpx.MockTransport(handler))
    assert status == 200 and len(body) == homepage.MAX_BODY_BYTES
    assert seen["headers"]["accept-encoding"] == "identity"
    assert "MMGAResearch" in seen["headers"]["user-agent"]


def test_http_get_returns_redirects_without_following():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://10.0.0.5/"}, stream=httpx.ByteStream(b""))

    status, headers, body = homepage._http_get("https://acme.com", transport=httpx.MockTransport(handler))
    assert status == 302 and headers["location"] == "http://10.0.0.5/" and body == b""
    assert len(calls) == 1


def test_http_get_stops_reading_after_the_deadline(monkeypatch):
    clock = iter([0.0, 100.0, 200.0, 300.0])
    monkeypatch.setattr(homepage.time, "monotonic", lambda: next(clock))
    handler = lambda request: httpx.Response(200, stream=httpx.ByteStream(b"y" * 10))
    status, _, body = homepage._http_get("https://acme.com", transport=httpx.MockTransport(handler))
    assert status == 200 and len(body) <= 10
