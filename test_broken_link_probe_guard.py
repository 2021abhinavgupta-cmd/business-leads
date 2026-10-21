"""Fix wave 2 item 3: the broken-link probe skips private hosts only. A link
whose domain does not resolve is a dead link and must still be reported."""
import socket

import pytest

import security_utils
from analyzer import visuals
from security_utils import link_probe_verdict


def _resolver(table):
    def resolve(host, port):
        if host not in table:
            raise socket.gaierror(11001, "getaddrinfo failed")
        return [(None, None, None, None, (table[host], 0))]
    return resolve


TABLE = {"www.example.com": "93.184.216.34", "intranet.example.com": "10.0.0.8",
         "cgnat.example.com": "100.100.100.200"}


@pytest.mark.parametrize("url,verdict", [
    ("https://www.example.com/about", "probe"),
    ("https://dead-domain-xyz.example/", "unresolvable"),
    ("https://intranet.example.com/", "skip"),
    ("https://cgnat.example.com/", "skip"),
    ("http://127.0.0.1/admin", "skip"),
    ("http://localhost:8000/", "skip"),
    ("http://2130706433/", "skip"),
])
def test_link_probe_verdict(url, verdict):
    assert link_probe_verdict(url, resolve=_resolver(TABLE)) == verdict


def test_a_resolver_error_other_than_nxdomain_fails_closed():
    def boom(host, port):
        raise OSError("resolver exploded")
    assert link_probe_verdict("https://www.example.com/", resolve=boom) == "skip"


class _Resp:
    def __init__(self, status):
        self.status = status


class _Request:
    def __init__(self, statuses):
        self.statuses, self.calls = statuses, []

    async def head(self, url, timeout=None):
        self.calls.append(("head", url))
        return _Resp(self.statuses[url])

    async def get(self, url, timeout=None):
        self.calls.append(("get", url))
        return _Resp(self.statuses[url])


class _Ctx:
    def __init__(self, statuses):
        self.request = _Request(statuses)


class _Page:
    def __init__(self, assets):
        self.assets = assets

    async def evaluate(self, script):
        return self.assets


async def test_a_dead_domain_link_is_reported_unreachable_and_a_private_one_is_skipped(monkeypatch):
    monkeypatch.setattr(security_utils.socket, "getaddrinfo", _resolver(TABLE))
    assets = [
        {"type": "link", "url": "https://www.example.com/ok", "text": "ok"},
        {"type": "link", "url": "https://www.example.com/gone", "text": "gone"},
        {"type": "link", "url": "https://dead-domain-xyz.example/", "text": "old shop"},
        {"type": "link", "url": "https://intranet.example.com/", "text": "staff"},
        {"type": "image", "url": "http://192.168.1.1/a.png", "text": "router"},
    ]
    ctx = _Ctx({"https://www.example.com/ok": 200, "https://www.example.com/gone": 404})
    broken, checked = await visuals._check_broken_assets(_Page(assets), ctx)

    by_url = {b["url"]: b["status"] for b in broken}
    assert by_url == {"https://www.example.com/gone": 404, "https://dead-domain-xyz.example/": "unreachable"}
    assert checked == 3
    probed = {u for _, u in ctx.request.calls}
    # The dead domain is never handed to the HTTP client (its name could be
    # made to resolve privately by the time the request is made); the private
    # hosts are never touched at all.
    assert probed == {"https://www.example.com/ok", "https://www.example.com/gone"}
