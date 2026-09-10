from scrapers.social.facebook import fetch_facebook


_PAGE_HTML = """
<html><head><title>Acme Candles | Facebook</title>
<meta property="og:title" content="Acme Candles">
<meta property="og:description" content="Handmade candles for calm homes.">
</head><body>
<div>12,340 people like this</div>
<div>12,900 people follow this</div>
</body></html>
"""

_LOGIN_WALL = "<html><body>You must log in to continue.</body></html>"


def test_reads_like_and_follow_counts():
    p = fetch_facebook("AcmeCandlesOfficial", http_get=lambda url: _PAGE_HTML)
    assert p.platform == "facebook"
    assert p.analyzed is True
    assert p.followers == 12900
    assert p.display_name == "Acme Candles"
    assert "Handmade candles" in p.bio


def test_login_wall_is_unanalyzed_not_none():
    p = fetch_facebook("AcmeCandlesOfficial", http_get=lambda url: _LOGIN_WALL)
    assert p is not None
    assert p.analyzed is False
    assert p.note


def test_hard_fetch_error_returns_none():
    def boom(url):
        raise RuntimeError("timeout")
    assert fetch_facebook("x", http_get=boom) is None


def test_accepts_full_url():
    p = fetch_facebook("https://www.facebook.com/AcmeCandlesOfficial/", http_get=lambda url: _PAGE_HTML)
    assert p.handle == "AcmeCandlesOfficial"
