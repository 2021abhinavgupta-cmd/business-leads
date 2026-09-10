"""
Instagram adapter — thin mapping from the existing InstagramScraper's
InstagramData onto the shared SocialProfile. All the scraping, session
handling and the challenge circuit breaker live in scrapers/instagram.py
and are unchanged.
"""

from scrapers.instagram import InstagramScraper
from scrapers.social.base import SocialProfile

_scraper = InstagramScraper()


def fetch_instagram(handle: str, scraper=None) -> SocialProfile | None:
    """Return a SocialProfile for *handle*, or None if the profile is not
    found or Instagram is currently in a challenge cooldown."""
    handle = handle.lstrip("@").strip()
    sc = scraper or _scraper
    data = sc.get_instagram_data(handle)
    if data is None:
        return None

    return SocialProfile(
        platform="instagram",
        handle=handle,
        url=f"https://instagram.com/{handle}",
        display_name=data.username,
        bio=data.bio,
        followers=data.followers,
        following=data.following,
        posts_count=data.posts_count,
        posts_last_30_days=data.posts_last_30_days,
        posting_frequency=data.posting_frequency,
        avg_engagement_rate=data.engagement_rate,
        uses_video=data.uses_reels,
        has_link_in_bio=data.has_link_in_bio,
        sample_captions=list(data.sample_captions),
    )
