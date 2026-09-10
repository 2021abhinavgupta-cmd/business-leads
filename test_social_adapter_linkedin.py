from scrapers.social.linkedin import fetch_linkedin


_CO_HTML = """
<html><head>
<meta property="og:title" content="Acme Candles | LinkedIn">
<meta property="og:description" content="Acme Candles makes hand poured candles. 5,200 followers.">
</head><body>
<div>5,200 followers</div>
<p>Acme Candles is a small studio making hand poured soy candles.</p>
</body></html>
"""

_AUTHWALL = "<html><body>Sign in to see more</body></html>"


def test_reads_follower_count_and_about():
    p = fetch_linkedin("acme-candles", http_get=lambda url: _CO_HTML)
    assert p.platform == "linkedin"
    assert p.analyzed is True
    assert p.followers == 5200
    assert "hand poured" in p.bio.lower()


def test_authwall_is_unanalyzed():
    p = fetch_linkedin("acme-candles", http_get=lambda url: _AUTHWALL)
    assert p.analyzed is False and p.note


def test_hard_error_returns_none():
    def boom(url):
        raise RuntimeError("999")
    assert fetch_linkedin("x", http_get=boom) is None


def test_accepts_company_url():
    p = fetch_linkedin("https://www.linkedin.com/company/acme-candles/", http_get=lambda url: _CO_HTML)
    assert p.handle == "acme-candles"
