"""
Facebook adapter — best effort only. There is no free Page API without Meta
app review, so this scrapes the public Page HTML. It works when Facebook
serves a real page with a visible like/follow count and returns
analyzed=False (never a crash) when it hits a login wall or unfamiliar
markup, which is common. Treat a miss here as normal.
"""

import re

import httpx
from bs4 import BeautifulSoup

from scrapers.social.base import SocialProfile, unanalyzed

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
_NUM = re.compile(r"([\d,.]+)\s*(?:people\s+)?(like|follow)", re.I)
_LOGIN_MARKERS = ("you must log in", "log in to continue", "log into facebook", "isn't available")


def _default_get(url: str) -> str:
    with httpx.Client(timeout=8, follow_redirects=True, headers={"User-Agent": _UA}) as c:
        r = c.get(url)
        return r.text


def _slug(s: str) -> str:
    s = (s or "").rstrip("/")
    if "facebook.com/" in s:
        s = s.split("facebook.com/", 1)[1]
    return s.split("/")[0].split("?")[0]


def _to_int(raw: str) -> int:
    raw = raw.replace(",", "").strip()
    m = re.match(r"([\d.]+)\s*([KkMm])?", raw)
    if not m:
        return 0
    n = float(m.group(1))
    suf = (m.group(2) or "").lower()
    return int(n * {"k": 1_000, "m": 1_000_000}.get(suf, 1))


def fetch_facebook(slug_or_url: str, http_get=None) -> SocialProfile | None:
    slug = _slug(slug_or_url)
    url = f"https://www.facebook.com/{slug}"
    get = http_get or _default_get
    try:
        html = get(url)
    except Exception as e:  # noqa: BLE001
        print(f"[Facebook] fetch failed: {e}")
        return None

    low = (html or "").lower()
    if not html or any(m in low for m in _LOGIN_MARKERS):
        return unanalyzed("facebook", slug, url, "Facebook served a login wall, could not read the public page")

    soup = BeautifulSoup(html, "html.parser")
    name = ""
    og_t = soup.find("meta", property="og:title")
    if og_t and og_t.get("content"):
        name = og_t["content"].split("|")[0].strip()
    bio = ""
    og_d = soup.find("meta", property="og:description")
    if og_d and og_d.get("content"):
        bio = og_d["content"].strip()

    likes = follows = 0
    for m in _NUM.finditer(soup.get_text(" ", strip=True)):
        val = _to_int(m.group(1))
        if m.group(2).lower().startswith("follow"):
            follows = max(follows, val)
        else:
            likes = max(likes, val)

    if not (likes or follows or name):
        return unanalyzed("facebook", slug, url, "Could not find a follower count on the public page")

    return SocialProfile(
        platform="facebook",
        handle=slug,
        url=url,
        display_name=name,
        bio=bio,
        followers=follows or likes,
        has_link_in_bio=False,
    )
