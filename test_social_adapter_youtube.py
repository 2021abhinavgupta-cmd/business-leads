from datetime import datetime, timedelta, timezone

import config
from scrapers.social.youtube import fetch_youtube


_CHANNEL_JSON = {
    "items": [{
        "snippet": {"title": "Acme Candles", "description": "Handmade candles. acme.com", "publishedAt": "2020-01-01T00:00:00Z"},
        "statistics": {"subscriberCount": "1500", "videoCount": "42", "viewCount": "230000"},
    }]
}
_now = datetime.now(timezone.utc)
_RECENT_JSON = {
    "items": [
        {"snippet": {"publishedAt": (_now - timedelta(days=4)).strftime("%Y-%m-%dT%H:%M:%SZ"), "title": "new scent"}},
        {"snippet": {"publishedAt": (_now - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ"), "title": "studio tour"}},
    ]
}


def test_none_when_key_unset(monkeypatch):
    monkeypatch.setattr(config, "YOUTUBE_API_KEY", None)
    assert fetch_youtube("@AcmeCandles") is None


def test_maps_channel_stats(monkeypatch):
    monkeypatch.setattr(config, "YOUTUBE_API_KEY", "k")

    def fake_get(url):
        return _CHANNEL_JSON if "channels" in url else _RECENT_JSON

    p = fetch_youtube("@AcmeCandles", http_get=fake_get)
    assert p.platform == "youtube"
    assert p.display_name == "Acme Candles"
    assert p.followers == 1500
    assert p.posts_count == 42
    assert p.uses_video is True
    assert p.bio.startswith("Handmade candles")
    assert p.posts_last_30_days >= 1
    assert p.analyzed is True


def test_none_when_channel_missing(monkeypatch):
    monkeypatch.setattr(config, "YOUTUBE_API_KEY", "k")
    p = fetch_youtube("@ghost", http_get=lambda url: {"items": []})
    assert p is None


def test_api_error_returns_none(monkeypatch):
    monkeypatch.setattr(config, "YOUTUBE_API_KEY", "k")

    def boom(url):
        raise RuntimeError("403")
    assert fetch_youtube("@x", http_get=boom) is None
