"""
SSRF guard for user-supplied URLs (audit endpoint takes an arbitrary website
URL and server-side fetches it via Playwright/httpx). Without this, a caller
with a valid API key could point the audit at internal infra (169.254.x for
cloud metadata, 10.x/172.16.x/192.168.x for internal services, localhost).
"""

import ipaddress
import re
import socket
from urllib.parse import urlparse


class UnsafeURLError(ValueError):
    pass


_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def is_plain_hostname(domain: str) -> bool:
    """Strict check for a bare DNS hostname (no scheme, port, path, userinfo, IP literal).
    Needed because URL parsers and browsers disagree on odd characters such as backslash."""
    if not isinstance(domain, str) or not domain or len(domain) > 253:
        return False
    labels = domain.split(".")
    # fullmatch, not match: "$" also matches before a trailing newline, so
    # "a.com\n" used to pass.
    if len(labels) < 2 or not all(_LABEL.fullmatch(label) for label in labels):
        return False
    if all(label.isdigit() for label in labels):  # dotted IPv4 literal
        return False
    return True


def validate_public_url(url: str) -> None:
    """Raise UnsafeURLError if *url* doesn't point at a public host."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURLError(f"Unsupported URL scheme: {parsed.scheme!r}")

    hostname = parsed.hostname
    if not hostname:
        raise UnsafeURLError("URL has no hostname")

    try:
        addrs = {info[4][0] for info in socket.getaddrinfo(hostname, None)}
    except socket.gaierror:
        raise UnsafeURLError(f"Could not resolve host: {hostname}")

    for addr in addrs:
        ip = ipaddress.ip_address(addr)
        if not _ip_is_public(ip):
            raise UnsafeURLError(f"URL resolves to a non-public address: {hostname} -> {addr}")


class UnsafeNavigationError(UnsafeURLError):
    """The capture browser was sent (directly or by a redirect) to a non-public host."""


_LOCAL_SCHEMES = ("data", "blob", "about")


def _ip_is_public(ip) -> bool:
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified
    )


def browser_url_allowed(url: str, *, navigation: bool, check_public=None) -> bool:
    """
    May the audit browser load *url*? data:, blob: and about: never touch the
    network and are allowed. Anything else must be http(s) to a host that is
    not localhost and not a non-public IP literal. A navigation (a document or
    frame load) is also resolved with validate_public_url, so a public looking
    name that resolves to a private address is refused; subresources skip the
    DNS lookup (too many of them) and get only the literal checks. Any error
    refuses (fail closed).
    """
    try:
        parsed = urlparse(url)
        scheme = (parsed.scheme or "").lower()
        if scheme in _LOCAL_SCHEMES:
            return True
        if scheme not in ("http", "https"):
            return False
        host = (parsed.hostname or "").rstrip(".").lower()
        if not host or host == "localhost" or host.endswith(".localhost"):
            return False
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip is not None and not _ip_is_public(ip):
            return False
        if navigation:
            (check_public or validate_public_url)(url)
        return True
    except Exception:
        return False
