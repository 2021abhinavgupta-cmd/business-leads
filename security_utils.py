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
    """Only globally routable unicast addresses are public. is_global is what
    refuses 100.64.0.0/10 (carrier NAT, Tailscale, Alibaba Cloud's metadata at
    100.100.100.200), which is_private does not cover; the explicit checks stay
    because is_global alone lets multicast through."""
    return ip.is_global and not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified
    )


_IPV4_PART = re.compile(r"^(?:0x[0-9a-f]*|[0-9]+)$")
_INVALID = object()


def _browser_ipv4(host: str):
    """
    Read *host* the way a browser's URL parser does (WHATWG "IPv4 parser"):
    1 to 4 dot separated parts, each decimal, octal (leading 0) or hex (0x),
    the last part filling the remaining bytes. So 2130706433, 0x7f000001,
    127.1 and 0177.0.0.1 are all 127.0.0.1. Returns an IPv4Address, None when
    the host is a name, or _INVALID when it looks numeric but is not a valid
    address (a browser refuses those; so do we).
    """
    parts = host.split(".")
    if parts and parts[-1] == "" and len(parts) > 1:
        parts.pop()
    if not parts or not _IPV4_PART.fullmatch(parts[-1]):
        return None
    if len(parts) > 4 or not all(_IPV4_PART.fullmatch(p) for p in parts):
        return _INVALID
    numbers = []
    for p in parts:
        if p.startswith("0x"):
            numbers.append(int(p[2:] or "0", 16))
        elif len(p) > 1 and p.startswith("0"):
            if not set(p) <= set("01234567"):
                return _INVALID
            numbers.append(int(p, 8))
        else:
            numbers.append(int(p))
    *head, last = numbers
    if any(n > 255 for n in head) or last >= 256 ** (5 - len(numbers)):
        return _INVALID
    value = last
    for i, n in enumerate(head):
        value += n << (8 * (3 - i))
    return ipaddress.IPv4Address(value)


def link_probe_verdict(url: str, *, resolve=None) -> str:
    """
    What the broken-link scan should do with *url*, whose HTTP client is not
    behind the browser's route guard:
      "probe"        resolves, and only to public addresses
      "unresolvable" the name does not exist (NXDOMAIN): a dead link, to be
                     reported without probing, since by request time the name
                     could be made to resolve somewhere private
      "skip"         localhost, a private IP literal or a name resolving to a
                     private address; any other error also skips (fail closed)
    """
    try:
        if not browser_url_allowed(url, navigation=False):
            return "skip"
        host = urlparse(url).hostname
        try:
            infos = (resolve or socket.getaddrinfo)(host, None)
        except socket.gaierror:
            return "unresolvable"
        addrs = {info[4][0] for info in infos}
        if not addrs:
            return "unresolvable"
        for addr in addrs:
            if not _ip_is_public(ipaddress.ip_address(str(addr).split("%", 1)[0])):
                return "skip"
        return "probe"
    except Exception:
        return "skip"


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
            ip = _browser_ipv4(host)
        if ip is _INVALID:
            return False
        if ip is not None and not _ip_is_public(ip):
            return False
        if navigation:
            (check_public or validate_public_url)(url)
        return True
    except Exception:
        return False
