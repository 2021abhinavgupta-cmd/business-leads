"""
Find a business's social handles by scraping its homepage for outbound
social links. Plain httpx GET (no Playwright, no semaphore) so it is cheap
and never contends with an audit's browser.
"""

import httpx
from bs4 import BeautifulSoup

_PLATFORM_HOSTS = {
    "instagram": "instagram.com",
    "facebook": "facebook.com",
    "linkedin": "linkedin.com",
    "youtube": "youtube.com",
}

# Substrings that mean "not the profile we want" per platform.
_REJECT = {
    "instagram": ("/p/", "/reel/", "/reels/", "/explore", "/stories/", "instagram.com/accounts"),
    "facebook": ("/sharer", "/dialog/", "/plugins/", "/tr?", "facebook.com/events"),
    "linkedin": ("/in/", "/pub/", "/sharearticle", "/sharing/"),
    "youtube": ("/watch", "/embed/", "/results", "/redirect"),
}
# LinkedIn we only want company pages.
_REQUIRE = {"linkedin": "/company/"}


def _extract_from_html(html: str) -> dict:
    out = {k: "" for k in _PLATFORM_HOSTS}
    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:  # noqa: BLE001
        return out
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        low = href.lower()
        if not low.startswith("http"):
            continue
        for plat, host in _PLATFORM_HOSTS.items():
            if out[plat]:
                continue
            if host not in low:
                continue
            if any(bad in low for bad in _REJECT.get(plat, ())):
                continue
            if plat in _REQUIRE and _REQUIRE[plat] not in low:
                continue
            out[plat] = href
    return out


async def find_social_handles(homepage_url: str, client=None) -> dict:
    empty = {k: "" for k in _PLATFORM_HOSTS}
    if not homepage_url:
        return empty
    try:
        if client is not None:
            resp = await client.get(homepage_url)
        else:
            async with httpx.AsyncClient(timeout=5, follow_redirects=True) as c:
                resp = await c.get(homepage_url, headers={"User-Agent": "Mozilla/5.0"})
        if getattr(resp, "status_code", 200) >= 400:
            return empty
        return _extract_from_html(resp.text)
    except Exception:  # noqa: BLE001
        return empty
