"""
Shared shape for every social platform adapter.

Each adapter (instagram.py, youtube.py, facebook.py, linkedin.py) takes a
handle or profile URL and returns a SocialProfile. Fields an adapter cannot
fill are left at their zero value, never guessed. An adapter that reached the
profile but could not extract usable data returns analyzed=False with a note
rather than raising.
"""

from dataclasses import dataclass, field


@dataclass
class SocialProfile:
    platform: str            # "instagram" | "youtube" | "facebook" | "linkedin"
    handle: str
    url: str
    display_name: str = ""
    bio: str = ""
    followers: int = 0       # subscribers for YouTube, page likes for Facebook
    following: int = 0
    posts_count: int = 0
    posts_last_30_days: int = 0
    posting_frequency: str = ""
    avg_engagement_rate: float = 0.0
    uses_video: bool = False
    has_link_in_bio: bool = False
    last_post_age_days: int | None = None
    sample_captions: list[str] = field(default_factory=list)
    analyzed: bool = True
    note: str = ""


def classify_frequency(posts_last_30_days: int) -> str:
    """Map post count in the last 30 days to a human label (same bands as
    scrapers/instagram.py's InstagramScraper._classify_frequency)."""
    if posts_last_30_days >= 20:
        return "daily"
    if posts_last_30_days >= 8:
        return "2-3x per week"
    if posts_last_30_days >= 4:
        return "weekly"
    if posts_last_30_days >= 1:
        return "irregular"
    return "inactive (no posts in 30 days)"


def unanalyzed(platform: str, handle: str, url: str, note: str) -> SocialProfile:
    """A profile the adapter reached but could not analyze — surfaced in the
    UI as a note, never as an error or a fabricated issue."""
    return SocialProfile(platform=platform, handle=handle, url=url, analyzed=False, note=note)
