from scrapers.instagram import InstagramData
from scrapers.social.instagram import fetch_instagram


class _FakeScraper:
    def __init__(self, data):
        self._data = data

    def get_instagram_data(self, handle):
        return self._data


def _ig_data(**over):
    base = dict(
        username="acme", followers=4000, following=300, posts_count=88,
        bio="Handmade candles. Link below.", posts_last_30_days=10,
        avg_likes=120.0, avg_comments=8.0, engagement_rate=3.2,
        uses_reels=True, has_link_in_bio=True, posting_frequency="2-3x per week",
        sample_captions=["new drop", "restock soon"],
        content_types={"reels": 4, "carousel": 3, "image": 3},
    )
    base.update(over)
    return InstagramData(**base)


def test_maps_instagram_data_to_socialprofile():
    p = fetch_instagram("acme", scraper=_FakeScraper(_ig_data()))
    assert p.platform == "instagram"
    assert p.handle == "acme"
    assert p.url == "https://instagram.com/acme"
    assert p.followers == 4000
    assert p.following == 300
    assert p.posts_count == 88
    assert p.posts_last_30_days == 10
    assert p.posting_frequency == "2-3x per week"
    assert p.avg_engagement_rate == 3.2
    assert p.uses_video is True
    assert p.has_link_in_bio is True
    assert p.sample_captions == ["new drop", "restock soon"]
    assert p.analyzed is True


def test_none_when_profile_not_found():
    assert fetch_instagram("ghost", scraper=_FakeScraper(None)) is None


def test_strips_leading_at_sign():
    p = fetch_instagram("@acme", scraper=_FakeScraper(_ig_data()))
    assert p.handle == "acme"
    assert p.url == "https://instagram.com/acme"
