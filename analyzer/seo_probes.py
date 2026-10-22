"""
Small deterministic search-visibility checks, ported in spirit from the
seo-analyzer project (MIT). Each one is a fact read from the HTML, a response
header or an HTTP probe; none is an opinion.

  * canonical tag points to a page that is missing, erroring or redirecting
  * canonical tag uses http:// on an https:// page
  * noindex sent in the X-Robots-Tag response header (hides the whole page
    from Google even when the HTML looks fine)
  * more than one <title> or meta description in the page head
  * a long redirect chain before the page loads, or an https -> http downgrade

Accuracy rules, same as the rest of this codebase: whenever a check cannot get
a clean answer (probe times out, host blocks bots, no data) it reports nothing.
Absence of data is never a finding. Descriptions are written in plain words
because they are quoted into emails.
"""
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

# Hosts that answer automated requests with 4xx whatever the page is.
_BLOCKING_STATUSES = {401, 403, 429, 999}
_PROBE_TIMEOUT = 10


def _finding(severity: str, description: str, key: str) -> dict:
    return {"key": key, "category": "seo", "severity": severity, "description": description}


def _norm(url: str) -> str:
    """Compare URLs without caring about a trailing slash or fragment."""
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc.lower()}{p.path.rstrip('/')}{'?' + p.query if p.query else ''}"


def canonical_url(html: str, base_url: str) -> str:
    """The absolute canonical URL declared in the page, or ''."""
    try:
        soup = BeautifulSoup(html or "", "html.parser")
        for link in soup.find_all("link"):
            rel = link.get("rel") or []
            rel = [rel] if isinstance(rel, str) else rel
            if "canonical" in [r.lower() for r in rel] and link.get("href"):
                return urljoin(base_url, link["href"].strip())
    except Exception:
        pass
    return ""


def html_findings(html: str, final_url: str) -> list[dict]:
    """Checks that need only the page's HTML and the URL it loaded from."""
    if not html or not final_url:
        return []
    out: list[dict] = []
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return []

    # Scoped to <head>: SVG icons carry their own <title> and are not the page's.
    head = soup.find("head")
    if head is not None:
        titles = [t for t in head.find_all("title") if t.parent is head]
        if len(titles) > 1:
            out.append(_finding(
                "medium",
                f"The page has {len(titles)} separate title tags, so search engines have to guess "
                "which one to show as your headline in results",
                "multiple_titles",
            ))
        descs = [m for m in head.find_all("meta") if (m.get("name") or "").lower() == "description"]
        if len(descs) > 1:
            out.append(_finding(
                "low",
                f"The page has {len(descs)} different meta descriptions, so Google may show the wrong "
                "summary under your link",
                "multiple_descriptions",
            ))

    canon = canonical_url(html, final_url)
    if canon and final_url.lower().startswith("https://") and canon.lower().startswith("http://"):
        out.append(_finding(
            "medium",
            "The page's canonical tag points to the insecure http:// version of the address, "
            "telling Google to prefer the less secure copy",
            "canonical_http",
        ))
    return out


def header_findings(response_headers: dict | None) -> list[dict]:
    """Checks on the response headers captured when the page loaded."""
    if not response_headers:
        return []
    lowered = {str(k).lower(): str(v) for k, v in response_headers.items()}
    value = lowered.get("x-robots-tag", "").lower()
    # "noindex" for everyone, or for Googlebot specifically; a rule aimed at
    # some other bot ("otherbot: noindex") is not about Google.
    parts = [p.strip() for p in value.split(",")]
    for part in parts:
        if "noindex" not in part:
            continue
        if ":" in part:
            agent = part.split(":", 1)[0].strip()
            if agent not in ("googlebot", "google", "all"):
                continue
        return [_finding(
            "high",
            "The server tells search engines not to list this page (a noindex instruction in the "
            "response headers), so it can stay out of Google even though the page itself looks fine",
            "x_robots_noindex",
        )]
    return []


async def probe_findings(client, start_url: str, final_url: str, html: str) -> list[dict]:
    """Network checks: the redirect chain, and where the canonical tag leads.

    `client` is an httpx.AsyncClient. Any failure yields no finding.
    """
    out: list[dict] = []

    try:
        resp = await client.get(start_url, timeout=_PROBE_TIMEOUT, follow_redirects=True)
        hops = list(resp.history)
        if start_url.lower().startswith("https://") and str(resp.url).lower().startswith("http://"):
            out.append(_finding(
                "high",
                "Visitors who open the secure https:// address are sent to the insecure http:// version",
                "https_downgrade",
            ))
        elif len(hops) >= 3:
            out.append(_finding(
                "low",
                f"Visitors are bounced through {len(hops)} redirects before the page appears, "
                "which slows the first load",
                "redirect_chain",
            ))
    except Exception:
        pass

    canon = canonical_url(html, final_url or start_url)
    if canon:
        out.extend(await _probe_canonical(client, canon))
    return out


async def _probe_canonical(client, canon: str) -> list[dict]:
    try:
        resp = await client.get(canon, timeout=_PROBE_TIMEOUT, follow_redirects=False)
    except Exception:
        return []
    status = resp.status_code
    if status in _BLOCKING_STATUSES:
        return []
    if status in (404, 410) or 500 <= status < 600:
        return [_finding(
            "medium",
            "The page's canonical tag points to an address that does not load "
            f"(error {status}), so Google is told to prefer a page that is not there",
            "canonical_broken",
        )]
    if 300 <= status < 400:
        target = resp.headers.get("location", "")
        if target and _norm(urljoin(canon, target)) == _norm(canon):
            return []  # trailing-slash or fragment-only redirect: not worth a claim
        return [_finding(
            "low",
            "The page's canonical tag points to an address that itself redirects somewhere else, "
            "which muddies which version Google should list",
            "canonical_redirects",
        )]
    return []
