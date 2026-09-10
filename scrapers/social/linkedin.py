"""
LinkedIn adapter — best effort only, and the least reliable of the four.
LinkedIn fights scraping harder than anyone; this reads the public company
page HTML when LinkedIn serves one, and returns analyzed=False the rest of
the time (an auth wall, a 999 status, unfamiliar markup). A miss here is
the common case, not a bug.
"""

import re

import httpx
from bs4 import BeautifulSoup

from scrapers.social.base import SocialProfile, unanalyzed

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
_FOLLOWERS = re.compile(r"([\d,.]+)\s*followers", re.I)
_WALL_MARKERS = ("sign in to see", "join linkedin", "authwall", "log in to continue")


def _default_get(url: str) -> str:
    with httpx.Client(timeout=8, follow_redirects=True, headers={"User-Agent": _UA}) as c:
        r = c.get(url)
        return r.text


def _slug(s: str) -> str:
    s = (s or "").rstrip("/")
    if "linkedin.com/company/" in s:
        s = s.split("linkedin.com/company/", 1)[1]
    return s.split("/")[0].split("?")[0]


def _to_int(raw: str) -> int:
    raw = raw.replace(",", "").strip()
    m = re.match(r"([\d.]+)\s*([KkMm])?", raw)
    if not m:
        return 0
    return int(float(m.group(1)) * {"k": 1_000, "m": 1_000_000}.get((m.group(2) or "").lower(), 1))


def fetch_linkedin(slug_or_url: str, http_get=None) -> SocialProfile | None:
    slug = _slug(slug_or_url)
    url = f"https://www.linkedin.com/company/{slug}/"
    get = http_get or _default_get
    try:
        html = get(url)
    except Exception as e:  # noqa: BLE001
        print(f"[LinkedIn] fetch failed: {e}")
        return None

    low = (html or "").lower()
    if not html or any(m in low for m in _WALL_MARKERS):
        return unanalyzed("linkedin", slug, url, "LinkedIn served an auth wall, could not read the public page")

    soup = BeautifulSoup(html, "html.parser")
    name = ""
    og_t = soup.find("meta", property="og:title")
    if og_t and og_t.get("content"):
        name = og_t["content"].split("|")[0].strip()
    bio = ""
    og_d = soup.find("meta", property="og:description")
    if og_d and og_d.get("content"):
        bio = og_d["content"].strip()
    body_p = soup.find("p")
    if body_p and len(body_p.get_text(strip=True)) > len(bio):
        bio = body_p.get_text(strip=True)

    followers = 0
    m = _FOLLOWERS.search(soup.get_text(" ", strip=True))
    if m:
        followers = _to_int(m.group(1))

    if not (followers or name):
        return unanalyzed("linkedin", slug, url, "Could not find a follower count on the public page")

    return SocialProfile(
        platform="linkedin",
        handle=slug,
        url=url,
        display_name=name,
        bio=bio,
        followers=followers,
        has_link_in_bio=False,
    )
