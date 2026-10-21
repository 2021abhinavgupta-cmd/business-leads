"""Fix wave 2 item 2: only globally routable addresses count as public, and
non-canonical IPv4 spellings are read the way a browser reads them."""
import ipaddress

import pytest

import security_utils
from security_utils import UnsafeURLError, browser_url_allowed, validate_public_url


def _never_called(url):
    raise AssertionError("an IP literal must be judged without DNS")


@pytest.mark.parametrize("addr", [
    "100.64.0.1",        # CGNAT / Tailscale
    "100.100.100.200",   # Alibaba Cloud metadata
    "198.18.0.1",        # benchmarking
    "192.0.0.1",         # IETF protocol assignments
    "224.0.0.1",         # multicast
    "::ffff:127.0.0.1",  # IPv4-mapped loopback
    "fc00::1",
])
def test_non_global_addresses_are_not_public(addr):
    assert security_utils._ip_is_public(ipaddress.ip_address(addr)) is False


@pytest.mark.parametrize("addr", ["8.8.8.8", "93.184.216.34", "2001:4860:4860::8888"])
def test_global_addresses_are_public(addr):
    assert security_utils._ip_is_public(ipaddress.ip_address(addr)) is True


@pytest.mark.parametrize("url", [
    "http://100.64.0.1/", "http://100.100.100.200/latest/meta-data/",
    "http://2130706433/", "http://0x7f000001/", "http://127.1/", "http://0177.0.0.1/",
    "http://0x7f.1/", "http://3232235777/",  # 192.168.1.1
    "http://4294967296/",  # out of range: not an address, refused
])
@pytest.mark.parametrize("navigation", [True, False])
def test_cgnat_and_non_canonical_private_ipv4_are_refused(url, navigation):
    assert browser_url_allowed(url, navigation=navigation, check_public=_never_called) is False


@pytest.mark.parametrize("url", ["http://134744072/", "http://8.8.8.8/", "http://0x08080808/"])
def test_a_public_ip_literal_is_allowed(url):
    assert browser_url_allowed(url, navigation=False, check_public=_never_called) is True


def test_validate_public_url_refuses_a_cgnat_resolution(monkeypatch):
    monkeypatch.setattr(security_utils.socket, "getaddrinfo",
                        lambda host, port: [(None, None, None, None, ("100.100.100.200", 0))])
    with pytest.raises(UnsafeURLError, match="non-public"):
        validate_public_url("https://metadata.example.com/")


def test_validate_public_url_allows_a_global_resolution(monkeypatch):
    monkeypatch.setattr(security_utils.socket, "getaddrinfo",
                        lambda host, port: [(None, None, None, None, ("93.184.216.34", 0))])
    validate_public_url("https://www.example.com/")
