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

class _Frame:
    def __init__(self, main=True):
        self.parent_frame = None if main else object()


class _Req:
    def __init__(self, url, nav=True, main=True):
        self.url, self._nav = url, nav
        self.resource_type = "document" if nav else "image"
        self.frame = _Frame(main)

    def is_navigation_request(self):
        return self._nav


class _Route:
    def __init__(self, url, nav=True, main=True):
        self.request = _Req(url, nav, main)
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


async def test_a_private_iframe_is_aborted_without_failing_the_audit():
    blocked = []
    handler = visuals._make_ssrf_route_handler(blocked, check_public=_public)
    r = _Route("http://localhost:3000/", nav=True, main=False)
    await handler(r)
    assert r.outcome[0] == "abort" and blocked == []


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


# ---- redirect hops the route guard never sees ---------------------------------

class _EvReq:
    def __init__(self, url, nav=True, redirected=True):
        self.url, self._nav = url, nav
        self.resource_type = "document" if nav else "image"
        self.redirected_from = object() if redirected else None

    def is_navigation_request(self):
        return self._nav


def test_the_hop_recorder_records_only_redirect_hops():
    hops = []
    on_request = visuals._make_redirect_hop_recorder(hops)
    on_request(_EvReq("https://www.example.com/", redirected=False))
    on_request(_EvReq("http://127.0.0.1/secret", nav=True))
    on_request(_EvReq("http://10.0.0.9/a.png", nav=False))
    assert hops == [("http://127.0.0.1/secret", True), ("http://10.0.0.9/a.png", False)]


def test_a_request_whose_redirect_state_cannot_be_read_is_recorded():
    class Weird(_EvReq):
        @property
        def redirected_from(self):
            raise RuntimeError("gone")

        @redirected_from.setter
        def redirected_from(self, v):
            pass
    hops = []
    visuals._make_redirect_hop_recorder(hops)(Weird("http://127.0.0.1/"))
    assert hops == [("http://127.0.0.1/", True)]


@pytest.mark.parametrize("hop", [
    ("http://127.0.0.1/secret", True), ("http://internal.corp/", True),
    ("http://169.254.169.254/latest", False), ("http://localhost/x.png", False),
])
def test_a_private_redirect_hop_is_a_violation(hop):
    with pytest.raises(UnsafeNavigationError, match="non-public address"):
        visuals._assert_redirect_hops_public(
            [("https://www.example.com/a", True), hop], check_public=_public)


def test_public_redirect_hops_pass():
    visuals._assert_redirect_hops_public(
        [("https://www.example.com/", True), ("https://cdn.example.com/x.png", False)],
        check_public=_public)
    visuals._assert_redirect_hops_public([], check_public=_public)


def test_the_capture_checks_hops_on_both_contexts_before_closing_the_browser():
    import inspect
    src = inspect.getsource(visuals._generate_audit_screenshot_once)
    assert src.count("_make_redirect_hop_recorder(redirect_hops)") == 2
    check = src.index("_assert_redirect_hops_public, redirect_hops")
    assert check < src.index("await browser.close()") < src.index("img.save(filepath")


async def test_real_browser_redirect_hops_to_a_private_host_are_caught():
    """A delayed script navigation and an iframe each go to a public URL that
    302s to a private one: the route guard lets both through (it never sees
    the hop), the recorder catches both."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    playwright_api = pytest.importorskip("playwright.async_api")

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.startswith("/redir"):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{port}/secret")
                self.end_headers()
                return
            body = {
                "/home": b"<script>setTimeout(()=>{location.href='/redir-main'},200)</script>",
                "/iframe": b"<iframe src='/redir-iframe'></iframe>",
            }.get(self.path, b"<h1>INTERNAL SECRET</h1>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        async with playwright_api.async_playwright() as p:
            try:
                browser = await p.chromium.launch(
                    args=["--host-resolver-rules=MAP public.example.com 127.0.0.1"])
            except Exception as e:
                pytest.skip(f"no chromium: {e}")
            for path in ("/home", "/iframe"):
                ctx = await browser.new_context(service_workers="block")
                blocked, hops = [], []
                await ctx.route("**/*", visuals._make_ssrf_route_handler(blocked, check_public=_public))
                ctx.on("request", visuals._make_redirect_hop_recorder(hops))
                page = await ctx.new_page()
                await page.goto(f"http://public.example.com:{port}{path}")
                await page.wait_for_timeout(1000)
                assert blocked == []  # the route guard alone misses it
                assert (f"http://127.0.0.1:{port}/secret", True) in hops
                with pytest.raises(UnsafeNavigationError):
                    visuals._assert_redirect_hops_public(hops, check_public=_public)
                await ctx.close()
            await browser.close()
    finally:
        srv.shutdown()
