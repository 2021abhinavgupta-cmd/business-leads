"""
Instagram scraper — pulls business profile & post analytics using instagrapi.

Authenticates via saved session (session.json) or IG_USERNAME / IG_PASSWORD
from config. Analyses the last 20 posts to compute engagement metrics,
content-type breakdown, and posting frequency.
"""

import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from instagrapi import Client
from instagrapi.exceptions import LoginRequired, UserNotFound, ChallengeRequired

import config

# ---------------------------------------------------------------------------
# Session file path (project root)
# ---------------------------------------------------------------------------
_SESSION_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "session.json")

# Anti-detection delay range (seconds)
_DELAY_MIN = 3
_DELAY_MAX = 5

# How long to stop hitting Instagram entirely after a ban/checkpoint signal
# (LoginRequired/ChallengeRequired). Retrying immediately after a challenge is
# what turns a soft flag into a permanent ban — this is a circuit breaker, not
# a fix for the underlying ToS risk of scraping Instagram at all.
_CHALLENGE_COOLDOWN_SECONDS = 60 * 60


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class InstagramData:
    """Structured analytics for a single Instagram profile."""

    username: str
    followers: int
    following: int
    posts_count: int
    bio: str
    posts_last_30_days: int
    avg_likes: float
    avg_comments: float
    engagement_rate: float
    uses_reels: bool
    has_link_in_bio: bool
    posting_frequency: str
    sample_captions: list[str] = field(default_factory=list)
    content_types: dict = field(default_factory=lambda: {
        "reels": 0, "carousel": 0, "image": 0,
    })


# ---------------------------------------------------------------------------
# Scraper class
# ---------------------------------------------------------------------------
class InstagramScraper:
    """Scrape Instagram business profiles via instagrapi."""

    def __init__(self):
        self.cl = Client()
        self._logged_in = False
        self._locked_until = 0.0

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _trip_challenge_breaker(self, context: str) -> None:
        """Stop all Instagram calls for a cooldown window after a ban/checkpoint signal."""
        self._logged_in = False
        self._locked_until = time.time() + _CHALLENGE_COOLDOWN_SECONDS
        print(
            f"[Instagram] Challenge/checkpoint hit during {context} — "
            f"pausing all Instagram calls for {_CHALLENGE_COOLDOWN_SECONDS // 60} minutes "
            f"to avoid escalating a soft flag into a ban."
        )

    def _ensure_logged_in(self) -> None:
        """
        Ensure the instagrapi client has an active session.

        1. Try loading a saved session from *session.json*.
        2. Fall back to fresh login with ``IG_USERNAME`` / ``IG_PASSWORD``.
        3. Persist the session after successful login.
        """
        if time.time() < self._locked_until:
            raise LoginRequired(
                f"Instagram account in cooldown after a challenge signal, "
                f"{int(self._locked_until - time.time())}s remaining"
            )

        if self._logged_in:
            return

        # Attempt to restore a saved session
        if os.path.exists(_SESSION_FILE):
            try:
                self.cl.load_settings(_SESSION_FILE)
                self.cl.login(config.IG_USERNAME, config.IG_PASSWORD)
                self._logged_in = True
                return
            except Exception:
                pass  # Session expired — fall through to fresh login

        # Fresh login
        self.cl.login(config.IG_USERNAME, config.IG_PASSWORD)
        self.cl.dump_settings(_SESSION_FILE)
        self._logged_in = True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_instagram_data(self, username: str) -> InstagramData | None:
        """
        Fetch profile info and recent-post analytics for *username*.

        Args:
            username: Instagram handle (without ``@``).

        Returns:
            An ``InstagramData`` instance, or ``None`` if the profile
            cannot be found or an error occurs.
        """
        try:
            self._ensure_logged_in()

            # Random delay for anti-detection
            time.sleep(random.uniform(_DELAY_MIN, _DELAY_MAX))

            user_id = self.cl.user_id_from_username(username)
            user_info = self.cl.user_info(user_id)
            medias = self.cl.user_medias(user_id, amount=20)

            return self._build_instagram_data(username, user_info, medias)

        except UserNotFound:
            return None
        except ChallengeRequired:
            self._trip_challenge_breaker("get_instagram_data")
            return None
        except LoginRequired:
            # Session died mid-run — force re-login on next call
            self._logged_in = False
            return None
        except Exception:
            return None

    def send_dm(self, target_username: str, message: str) -> bool:
        """
        Send a direct message to a target username.
        """
        try:
            self._ensure_logged_in()
            # Delay to avoid anti-spam
            time.sleep(random.uniform(10, 20)) # Longer delay for DMs
            user_id = self.cl.user_id_from_username(target_username)
            self.cl.direct_send(message, user_ids=[user_id])
            return True
        except ChallengeRequired:
            self._trip_challenge_breaker("send_dm")
            return False
        except LoginRequired:
            self._logged_in = False
            return False
        except UserNotFound:
            print(f"Target IG username {target_username} not found.")
            return False
        except Exception as e:
            print(f"Error sending DM to {target_username}: {e}")
            return False

    # ------------------------------------------------------------------
    # Analytics builder
    # ------------------------------------------------------------------

    def _build_instagram_data(
        self, username: str, user_info, medias: list
    ) -> InstagramData:
        """
        Crunch the numbers from *user_info* and *medias* into an
        ``InstagramData`` instance.
        """
        followers = user_info.follower_count or 0
        following = user_info.following_count or 0
        posts_count = user_info.media_count or 0
        bio = user_info.biography or ""
        has_link = bool(user_info.external_url)

        # ----- 30-day window -----
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        recent = [m for m in medias if m.taken_at and m.taken_at >= cutoff]
        posts_last_30 = len(recent)

        # ----- Engagement -----
        total_likes = 0
        total_comments = 0
        content_types = {"reels": 0, "carousel": 0, "image": 0}
        uses_reels = False
        sample_captions: list[str] = []

        for media in medias:
            total_likes += media.like_count or 0
            total_comments += media.comment_count or 0

            # media_type: 1 = photo, 2 = video/reel, 8 = carousel
            if media.media_type == 1:
                content_types["image"] += 1
            elif media.media_type == 2:
                content_types["reels"] += 1
                uses_reels = True
            elif media.media_type == 8:
                content_types["carousel"] += 1

            caption = (media.caption_text or "").strip()
            if caption and len(sample_captions) < 5:
                sample_captions.append(caption[:200])

        media_count = len(medias) or 1  # avoid division by zero
        avg_likes = total_likes / media_count
        avg_comments = total_comments / media_count

        if followers > 0:
            engagement_rate = round(
                (avg_likes + avg_comments) / followers * 100, 2
            )
        else:
            engagement_rate = 0.0

        # ----- Posting frequency -----
        posting_frequency = self._classify_frequency(posts_last_30)

        return InstagramData(
            username=username,
            followers=followers,
            following=following,
            posts_count=posts_count,
            bio=bio,
            posts_last_30_days=posts_last_30,
            avg_likes=round(avg_likes, 1),
            avg_comments=round(avg_comments, 1),
            engagement_rate=engagement_rate,
            uses_reels=uses_reels,
            has_link_in_bio=has_link,
            posting_frequency=posting_frequency,
            sample_captions=sample_captions,
            content_types=content_types,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_frequency(posts_last_30: int) -> str:
        """Map the number of posts in the last 30 days to a human label."""
        if posts_last_30 >= 20:
            return "daily"
        if posts_last_30 >= 8:
            return "2-3x per week"
        if posts_last_30 >= 4:
            return "weekly"
        if posts_last_30 >= 1:
            return "irregular"
        return "inactive (no posts in 30 days)"
