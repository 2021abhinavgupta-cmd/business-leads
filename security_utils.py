"""
SSRF guard for user-supplied URLs (audit endpoint takes an arbitrary website
URL and server-side fetches it via Playwright/httpx). Without this, a caller
with a valid API key could point the audit at internal infra (169.254.x for
cloud metadata, 10.x/172.16.x/192.168.x for internal services, localhost).
"""

import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeURLError(ValueError):
    pass


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
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise UnsafeURLError(f"URL resolves to a non-public address: {hostname} -> {addr}")
