"""
Fetch a company's homepage text for the import drafter.

The domain comes from an uploaded CSV, so it is untrusted input: every hop is
validated against security_utils.validate_public_url (the same SSRF guard the
audit endpoint uses) and redirects are followed by hand, at most three, so a
public host cannot bounce us onto an internal address. Any failure returns
None; drafting then proceeds from the CSV fields alone and the draft is
flagged "no homepage evidence".
"""
import time
from urllib.parse import urljoin

import httpx
import trafilatura

from security_utils import UnsafeURLError, validate_public_url

MAX_REDIRECTS = 3
FETCH_TIMEOUT_SECONDS = 8.0
MAX_BODY_BYTES = 1_000_000
MAX_TEXT_CHARS = 4000
_USER_AGENT = "Mozilla/5.0 (compatible; MMGAResearch/1.0)"
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)


def _http_get(url: str, transport=None) -> tuple[int, dict, bytes]:
    """
    One GET, no redirect following, body capped at MAX_BODY_BYTES and read for
    at most FETCH_TIMEOUT_SECONDS in total. Compression is refused
    (Accept-Encoding identity) so a small download cannot inflate into a huge one.
    """
    deadline = time.monotonic() + FETCH_TIMEOUT_SECONDS
    with httpx.Client(
        follow_redirects=False, timeout=FETCH_TIMEOUT_SECONDS, transport=transport,
        headers={"User-Agent": _USER_AGENT, "Accept-Encoding": "identity"},
    ) as client:
        with client.stream("GET", url) as response:
            body = b""
            for chunk in response.iter_raw():
                body += chunk
                if len(body) >= MAX_BODY_BYTES:
                    body = body[:MAX_BODY_BYTES]
                    break
                if time.monotonic() > deadline:
                    break
            return response.status_code, dict(response.headers), body


def fetch_homepage_text(domain: str, get=_http_get) -> str | None:
    if not domain:
        return None
    url = f"https://{domain}"
    try:
        for _ in range(MAX_REDIRECTS + 1):
            validate_public_url(url)
            status, headers, body = get(url)
            if status in _REDIRECT_STATUSES:
                location = headers.get("location") or headers.get("Location")
                if not location:
                    return None
                url = urljoin(url, location)
                continue
            if status != 200 or not body:
                return None
            html = body[:MAX_BODY_BYTES].decode("utf-8", errors="replace")
            text = " ".join((trafilatura.extract(html) or "").split())
            return text[:MAX_TEXT_CHARS] or None
        return None
    except UnsafeURLError as e:
        print(f"[Import] Refused homepage fetch for {domain}: {e}")
        return None
    except Exception as e:
        print(f"[Import] Homepage fetch failed for {domain}: {e}")
        return None
