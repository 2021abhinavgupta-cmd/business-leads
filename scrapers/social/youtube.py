"""
YouTube adapter — official YouTube Data API v3 (free 10,000 units/day).
channels.list is ~1 to 3 units; we resolve a channel by handle or id
directly and avoid the 100-unit search endpoint. No new dependency; plain
httpx GET.
"""

from datetime import datetime, timezone

import httpx

import config
from scrapers.social.base import SocialProfile, classify_frequency

_API = "https://www.googleapis.com/youtube/v3"


def _default_get(url: str) -> dict:
    with httpx.Client(timeout=8) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.json()


def _channels_url(handle_or_id: str, key: str) -> str:
    h = handle_or_id.strip()
    parts = "snippet,statistics,contentDetails"
    if h.startswith("@"):
        return f"{_API}/channels?part={parts}&forHandle={h}&key={key}"
    if h.startswith("UC") and len(h) > 20:
        return f"{_API}/channels?part={parts}&id={h}&key={key}"
    return f"{_API}/channels?part={parts}&forHandle=@{h}&key={key}"


def fetch_youtube(handle_or_id: str, http_get=None) -> SocialProfile | None:
    key = config.YOUTUBE_API_KEY
    if not key:
        return None
    get = http_get or _default_get
    try:
        data = get(_channels_url(handle_or_id, key))
    except Exception as e:  # noqa: BLE001
        print(f"[YouTube] channels.list failed: {e}")
        return None

    items = (data or {}).get("items") or []
    if not items:
        return None
    ch = items[0]
    snip = ch.get("snippet", {})
    stats = ch.get("statistics", {})
    uploads = ch.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads", "")
    channel_id = ch.get("id", "")

    posts_last_30 = 0
    last_age = None
    try:
        if uploads:
            pl = get(f"{_API}/playlistItems?part=snippet&maxResults=20&playlistId={uploads}&key={key}")
        else:
            pl = get(f"{_API}/search?part=snippet&channelId={channel_id}&order=date&maxResults=20&type=video&key={key}")
        now = datetime.now(timezone.utc)
        dates = []
        for it in (pl or {}).get("items", []):
            ts = it.get("snippet", {}).get("publishedAt")
            if not ts:
                continue
            dates.append(datetime.fromisoformat(ts.replace("Z", "+00:00")))
        if dates:
            last_age = (now - max(dates)).days
            posts_last_30 = sum(1 for d in dates if (now - d).days <= 30)
    except Exception as e:  # noqa: BLE001
        print(f"[YouTube] recent-uploads lookup failed (non fatal): {e}")

    handle = handle_or_id.lstrip("@")
    return SocialProfile(
        platform="youtube",
        handle=handle,
        url=f"https://youtube.com/@{handle}",
        display_name=snip.get("title", ""),
        bio=snip.get("description", ""),
        followers=int(stats.get("subscriberCount", 0) or 0),
        posts_count=int(stats.get("videoCount", 0) or 0),
        posts_last_30_days=posts_last_30,
        posting_frequency=classify_frequency(posts_last_30),
        uses_video=True,
        has_link_in_bio=False,
        last_post_age_days=last_age,
    )
