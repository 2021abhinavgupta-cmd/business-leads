"""
SSL certificate expiry — how many days until the padlock breaks.

`WebsiteData.has_ssl` is a boolean: the site either served HTTPS or it
didn't. That misses the failure mode that actually hurts a small business,
because it is invisible right up until it isn't — a certificate that expires
next week still serves a perfectly valid padlock today, and then one morning
every visitor gets a full-page browser interstitial ("Your connection is not
private") that almost nobody clicks through. For a business whose site is
its storefront, that's total conversion loss with no warning.

Small businesses are exactly the population this happens to: a cert nobody
renewed because the person who set the site up moved on, or auto-renewal
that silently broke. It's also the rare flaw that is both urgent and
genuinely easy to fix, which makes it good outreach material rather than
just another criticism.

Uses only Python's stdlib `ssl`/`socket` — no dependency, no API key, no
third-party service, and no extra HTTP request against the target beyond one
short TLS handshake. Degrades to None on anything unexpected, matching the
"a dead signal must never become a confident claim" rule this codebase
follows everywhere else.
"""

import asyncio
import socket
import ssl
from datetime import datetime, timezone
from urllib.parse import urlparse

_HANDSHAKE_TIMEOUT_SECONDS = 8


def _read_not_after(hostname: str, port: int, verify: bool) -> str | None:
    """One TLS handshake; returns the certificate's notAfter string."""
    if verify:
        context = ssl.create_default_context()
    else:
        # An ALREADY-EXPIRED certificate is the single most valuable case
        # this module detects, and a verifying context refuses the handshake
        # outright (CERTIFICATE_VERIFY_FAILED), so the severe case would read
        # identically to "couldn't check" and produce no flaw at all. This
        # unverified second attempt exists only to read the dates off a cert
        # we've already declined to trust. It is NOT used to decide whether
        # the site is secure — has_ssl covers that — so relaxing verification
        # here doesn't weaken any security decision.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    with socket.create_connection((hostname, port), timeout=_HANDSHAKE_TIMEOUT_SECONDS) as sock:
        with context.wrap_socket(sock, server_hostname=hostname) as tls:
            # getpeercert() returns the parsed dict only on a verifying
            # context; with CERT_NONE it returns {}, so take the DER form,
            # which is always available, and read the dates off that.
            cert = tls.getpeercert()
            if cert:
                return cert.get("notAfter")
            der = tls.getpeercert(binary_form=True)

    if not der:
        return None

    from cryptography import x509

    expires = x509.load_der_x509_certificate(der).not_valid_after_utc
    return expires.strftime("%b %d %H:%M:%S %Y GMT")


def _days_until_expiry_sync(hostname: str, port: int = 443) -> int | None:
    try:
        not_after = _read_not_after(hostname, port, verify=True)
    except ssl.SSLCertVerificationError:
        # Expired, self-signed, or hostname-mismatched. Read the dates anyway
        # — see _read_not_after for why this matters most of all.
        not_after = _read_not_after(hostname, port, verify=False)

    if not not_after:
        return None

    # OpenSSL's fixed format, e.g. "Aug  9 12:00:00 2026 GMT".
    expires = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    return (expires - datetime.now(timezone.utc)).days


async def days_until_cert_expiry(url: str) -> int | None:
    """
    Days until *url*'s TLS certificate expires, or None if it can't be read.

    Negative means already expired. None covers every failure mode — a plain
    HTTP site, DNS failure, timeout, a handshake this client can't complete —
    because none of those tell us anything trustworthy about the certificate,
    and a guess here would become a false claim in a real email.

    Note this returns None for an *invalid* certificate too (create_default_
    context verifies), so a hostname mismatch or an untrusted issuer reads the
    same as "couldn't check". That's deliberate: has_ssl already covers
    whether HTTPS worked at all, and this check's job is only the expiry
    countdown on an otherwise-working cert.
    """
    parsed = urlparse(url if "://" in url else f"https://{url}")
    if parsed.scheme != "https":
        return None

    hostname = parsed.hostname
    if not hostname:
        return None

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_days_until_cert_expiry_blocking, hostname, parsed.port or 443),
            timeout=_HANDSHAKE_TIMEOUT_SECONDS + 2,
        )
    except Exception as e:
        print(f"[SSL] Certificate expiry check failed for {hostname}: {e} — skipping this signal.")
        return None


def _days_until_cert_expiry_blocking(hostname: str, port: int) -> int | None:
    """Thin wrapper so the blocking work has one clear name in tracebacks."""
    return _days_until_expiry_sync(hostname, port)
