"""Fix wave I2: the capture browser never loads a non-public host, redirects included.

No browser and no DNS: the public check is injected, and the Playwright route
and page objects are small fakes.
"""
import pytest

from analyzer import visuals
from importer import audit_runner
from security_utils import UnsafeNavigationError, UnsafeURLError, browser_url_allowed


def _public(url):
    """Stands in for validate_public_url: only names under example.com are public."""
    host = url.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    if not host.endswith("example.com"):
        raise UnsafeURLError(host)


BLOCKED = [
    "http://127.0.0.1/", "https://127.0.0.1:8443/admin", "http://192.168.1.10/", "http://10.0.0.5/",
    "http://172.16.3.4/", "http://localhost/", "http://localhost:8000/x", "http://app.localhost/",
    "http://169.254.169.254/latest/meta-data/", "http://[::1]/", "http://0.0.0.0/",
    "file:///etc/passwd", "ftp://example.com/",
]


@pytest.mark.parametrize("url", BLOCKED)
def test_non_public_navigation_is_blocked(url):
    assert browser_url_allowed(url, navigation=True, check_public=_public) is False


@pytest.mark.parametrize("url", [u for u in BLOCKED if u.startswith("http")])
def test_non_public_ip_literals_and_localhost_are_blocked_for_subresources_too(url):
    # Subresources skip the DNS lookup, but a literal private address or
    # localhost is refused without one.
    assert browser_url_allowed(url, navigation=False, check_public=_public) is False


def test_a_public_host_is_allowed():
    assert browser_url_allowed("https://www.example.com/about", navigation=True, check_public=_public)
    assert browser_url_allowed("https://cdn.example.com/a.png", navigation=False, check_public=_public)


def test_a_name_that_resolves_to_a_private_address_is_blocked_for_navigation():
    def resolves_private(url):
        raise UnsafeURLError("internal.corp -> 10.0.0.1")
    assert browser_url_allowed("https://internal.corp/", navigation=True, check_public=resolves_private) is False


def test_a_failing_public_check_fails_closed():
    def boom(url):
        raise OSError("dns down")
    assert browser_url_allowed("https://www.example.com/", navigation=True, check_public=boom) is False


@pytest.mark.parametrize("url", ["data:image/png;base64,AAAA", "blob:https://www.example.com/1", "about:blank"])
def test_local_schemes_never_touch_the_network_and_are_allowed(url):
    assert browser_url_allowed(url, navigation=False, check_public=_public) is True


# ---- the route handler -----------------------------------------------------

class _Req:
    def __init__(self, url, nav=True):
        self.url, self._nav = url, nav
        self.resource_type = "document" if nav else "image"

    def is_navigation_request(self):
        return self._nav


class _Route:
    def __init__(self, url, nav=True):
        self.request = _Req(url, nav)
        self.outcome = None

    async def abort(self, code=None):
        self.outcome = ("abort", code)

    async def continue_(self):
        self.outcome = ("continue", None)


async def test_route_guard_aborts_a_private_navigation_and_continues_a_public_one():
    blocked = []
    handler = visuals._make_ssrf_route_handler(blocked, check_public=_public)
    bad, good = _Route("http://169.254.169.254/latest"), _Route("https://www.example.com/")
    await handler(bad)
    await handler(good)
    assert bad.outcome[0] == "abort" and good.outcome == ("continue", None)
    assert blocked == ["http://169.254.169.254/latest"]


async def test_route_guard_aborts_a_private_subresource():
    handler = visuals._make_ssrf_route_handler([], check_public=_public)
    r = _Route("http://192.168.0.1/pixel.gif", nav=False)
    await handler(r)
    assert r.outcome[0] == "abort"


# ---- the final URL and redirect chain check ---------------------------------

class _HopReq:
    def __init__(self, url, prev=None):
        self.url, self.redirected_from = url, prev


class _Resp:
    def __init__(self, *urls):
        req = None
        for u in urls:
            req = _HopReq(u, req)
        self.request = req


def test_a_redirect_that_ends_on_a_private_host_is_a_violation():
    resp = _Resp("https://www.example.com/", "http://127.0.0.1/")
    with pytest.raises(UnsafeNavigationError):
        visuals._assert_public_navigation("http://127.0.0.1/", resp, check_public=_public)


def test_a_private_hop_in_the_middle_of_a_redirect_chain_is_a_violation():
    resp = _Resp("https://www.example.com/", "http://10.0.0.7/", "https://www.example.com/home")
    with pytest.raises(UnsafeNavigationError):
        visuals._assert_public_navigation("https://www.example.com/home", resp, check_public=_public)


def test_a_public_redirect_chain_passes():
    resp = _Resp("http://example.com/", "https://www.example.com/")
    visuals._assert_public_navigation("https://www.example.com/", resp, check_public=_public)
    visuals._assert_public_navigation("https://www.example.com/", None, check_public=_public)


# ---- what callers see ---------------------------------------------------------

async def test_wrapper_does_not_retry_an_unsafe_navigation(monkeypatch):
    calls = []

    async def once(url, company_name, **kw):
        calls.append(url)
        raise UnsafeNavigationError("redirected to http://127.0.0.1/")
    monkeypatch.setattr(visuals, "_generate_audit_screenshot_once", once)
    assert await visuals.generate_audit_screenshot("https://a.example.com", "A") == (None, None, None)
    assert len(calls) == 1
    with pytest.raises(UnsafeNavigationError):
        await visuals.generate_audit_screenshot("https://a.example.com", "A", raise_on_unsafe_navigation=True)


async def test_import_audit_fails_with_a_clear_error_and_no_shots(monkeypatch):
    monkeypatch.setattr(audit_runner, "_check_url", lambda url: None)
    seen = {}

    async def capture(url, company_name, **kw):
        seen.update(kw)
        raise UnsafeNavigationError("redirected to http://169.254.169.254/")
    res = await audit_runner.audit_one({"id": 3, "domain": "acme.com", "organization": "Acme"},
                                       capture=capture, judge=lambda b, d: True)
    assert seen.get("raise_on_unsafe_navigation") is True
    assert res["audit_status"] == "failed" and res["flaw_shots"] == []
    assert "non-public address" in res["audit_error"]
