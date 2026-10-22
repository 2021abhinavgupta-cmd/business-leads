"""Deterministic search-visibility checks (ported in spirit from seo-analyzer, MIT).
Silence beats a wrong claim: every check that cannot get a clean answer says nothing."""
import httpx
import pytest

from analyzer import seo_probes
from analyzer.seo_probes import header_findings, html_findings, probe_findings

PAGE = "https://acme.com/"


def _keys(findings):
    return [f["key"] for f in findings]


def _html(head: str) -> str:
    return f"<html><head>{head}</head><body><h1>Acme</h1></body></html>"


# --- html checks -------------------------------------------------------------

def test_two_title_tags_in_the_head_are_found():
    out = html_findings(_html("<title>A</title><title>B</title>"), PAGE)
    assert _keys(out) == ["multiple_titles"]


def test_an_svg_title_is_not_a_second_page_title():
    html = "<html><head><title>A</title></head><body><svg><title>Logo</title></svg></body></html>"
    assert html_findings(html, PAGE) == []


def test_two_meta_descriptions_are_found():
    head = '<title>A</title><meta name="description" content="x"><meta name="Description" content="y">'
    assert _keys(html_findings(_html(head), PAGE)) == ["multiple_descriptions"]


def test_an_https_page_with_an_http_canonical_is_found():
    out = html_findings(_html('<title>A</title><link rel="canonical" href="http://acme.com/">'), PAGE)
    assert _keys(out) == ["canonical_http"]


def test_an_http_page_with_an_http_canonical_is_fine():
    out = html_findings(_html('<link rel="canonical" href="http://acme.com/">'), "http://acme.com/")
    assert out == []


def test_a_relative_canonical_is_resolved_against_the_page():
    assert seo_probes.canonical_url(_html('<link rel="canonical" href="/about">'), PAGE) == "https://acme.com/about"


@pytest.mark.parametrize("html,url", [("", PAGE), (None, PAGE), ("<p>x</p>", ""), ("<p>no head</p>", PAGE)])
def test_missing_input_reports_nothing(html, url):
    assert html_findings(html, url) == []


# --- header check ------------------------------------------------------------

@pytest.mark.parametrize("value", ["noindex", "noindex, nofollow", "googlebot: noindex", "NOINDEX"])
def test_a_noindex_header_is_found(value):
    assert _keys(header_findings({"X-Robots-Tag": value})) == ["x_robots_noindex"]


@pytest.mark.parametrize("value", ["index, follow", "otherbot: noindex", "nofollow", ""])
def test_other_values_are_not_a_finding(value):
    assert header_findings({"x-robots-tag": value}) == []


@pytest.mark.parametrize("headers", [None, {}, {"server": "nginx"}])
def test_no_header_data_reports_nothing(headers):
    assert header_findings(headers) == []


# --- network probes (fake transport) ------------------------------------------

def _client(routes: dict):
    """routes: url -> (status, headers, [redirect urls leading here])."""
    def handler(request: httpx.Request) -> httpx.Response:
        status, headers = routes[str(request.url)]
        return httpx.Response(status, headers=headers, request=request)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


CANON_HTML = _html('<link rel="canonical" href="https://acme.com/">')


async def test_a_clean_site_has_no_findings():
    async with _client({PAGE: (200, {}), "https://acme.com/": (200, {})}) as c:
        assert await probe_findings(c, PAGE, PAGE, CANON_HTML) == []


async def test_a_canonical_that_404s_is_found():
    routes = {PAGE: (200, {}), "https://acme.com/": (200, {}), "https://acme.com/old": (404, {})}
    html = _html('<link rel="canonical" href="https://acme.com/old">')
    async with _client(routes) as c:
        assert _keys(await probe_findings(c, PAGE, PAGE, html)) == ["canonical_broken"]


async def test_a_canonical_that_redirects_elsewhere_is_found():
    routes = {PAGE: (200, {}), "https://acme.com/a": (301, {"location": "https://acme.com/b"})}
    html = _html('<link rel="canonical" href="https://acme.com/a">')
    async with _client(routes) as c:
        assert _keys(await probe_findings(c, PAGE, PAGE, html)) == ["canonical_redirects"]


async def test_a_trailing_slash_redirect_is_not_worth_a_claim():
    routes = {PAGE: (200, {}), "https://acme.com/a": (301, {"location": "https://acme.com/a/"})}
    html = _html('<link rel="canonical" href="https://acme.com/a">')
    async with _client(routes) as c:
        assert await probe_findings(c, PAGE, PAGE, html) == []


@pytest.mark.parametrize("status", [401, 403, 429])
async def test_a_blocking_status_on_the_canonical_is_not_a_finding(status):
    routes = {PAGE: (200, {}), "https://acme.com/a": (status, {})}
    html = _html('<link rel="canonical" href="https://acme.com/a">')
    async with _client(routes) as c:
        assert await probe_findings(c, PAGE, PAGE, html) == []


async def test_a_long_redirect_chain_is_found():
    routes = {
        "http://acme.com/": (301, {"location": "https://acme.com/"}),
        "https://acme.com/": (301, {"location": "https://www.acme.com/"}),
        "https://www.acme.com/": (302, {"location": "https://www.acme.com/home"}),
        "https://www.acme.com/home": (200, {}),
    }
    async with _client(routes) as c:
        out = await probe_findings(c, "http://acme.com/", "https://www.acme.com/home", _html(""))
    assert _keys(out) == ["redirect_chain"] and "3 redirects" in out[0]["description"]


async def test_the_ordinary_two_hop_http_to_https_to_www_is_left_alone():
    routes = {
        "http://acme.com/": (301, {"location": "https://acme.com/"}),
        "https://acme.com/": (301, {"location": "https://www.acme.com/"}),
        "https://www.acme.com/": (200, {}),
    }
    async with _client(routes) as c:
        assert await probe_findings(c, "http://acme.com/", "https://www.acme.com/", _html("")) == []


async def test_an_https_start_that_lands_on_http_is_found():
    routes = {PAGE: (301, {"location": "http://acme.com/"}), "http://acme.com/": (200, {})}
    async with _client(routes) as c:
        assert _keys(await probe_findings(c, PAGE, "http://acme.com/", _html(""))) == ["https_downgrade"]


async def test_a_network_failure_reports_nothing():
    def boom(request):
        raise httpx.ConnectError("down")
    async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as c:
        assert await probe_findings(c, PAGE, PAGE, CANON_HTML) == []


# --- wiring into the ranked flaw list ------------------------------------------

def test_findings_become_flaws_with_their_severity():
    from test_build_flaws import _build
    flaws = _build(seo_probe_findings=[
        {"key": "x_robots_noindex", "category": "seo", "severity": "high", "description": "Hidden from Google"},
        {"key": "redirect_chain", "category": "seo", "severity": "low", "description": "Too many redirects"},
    ])
    found = {f.description: f.severity for f in flaws}
    assert found["Hidden from Google."] == "high" and found["Too many redirects."] == "low"


def test_no_findings_adds_no_flaws():
    from test_build_flaws import _build
    assert _build(seo_probe_findings=[]) == []
