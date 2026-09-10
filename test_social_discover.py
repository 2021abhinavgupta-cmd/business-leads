import pytest
from scrapers.social.discover import find_social_handles, _extract_from_html


_HTML = """
<html><body>
  <a href="https://www.instagram.com/acmecandles/">IG</a>
  <a href="https://instagram.com/p/Cxyz123/">a post</a>
  <a href="https://www.facebook.com/AcmeCandlesOfficial">FB</a>
  <a href="https://www.facebook.com/sharer/sharer.php?u=x">share</a>
  <a href="https://www.linkedin.com/company/acme-candles/">LI company</a>
  <a href="https://www.linkedin.com/in/jane-doe/">LI person</a>
  <a href="https://www.youtube.com/@AcmeCandles">YT</a>
</body></html>
"""


def test_extract_picks_the_right_link_per_platform():
    got = _extract_from_html(_HTML)
    assert "instagram.com/acmecandles" in got["instagram"]
    assert got["facebook"].endswith("AcmeCandlesOfficial")
    assert "linkedin.com/company/acme-candles" in got["linkedin"]
    assert "youtube.com/@AcmeCandles" in got["youtube"]


def test_post_links_and_personal_profiles_ignored():
    got = _extract_from_html(_HTML)
    assert "/p/" not in got["instagram"]
    assert "/in/" not in got["linkedin"]
    assert "sharer" not in got["facebook"]


def test_missing_platform_is_empty_string():
    got = _extract_from_html("<html><body><a href='https://instagram.com/acme'>x</a></body></html>")
    assert got["youtube"] == ""
    assert got["facebook"] == ""


def test_first_match_wins():
    html = "<a href='https://instagram.com/first'>1</a><a href='https://instagram.com/second'>2</a>"
    assert "instagram.com/first" in _extract_from_html(html)["instagram"]


@pytest.mark.asyncio
async def test_fetch_error_returns_all_empty():
    class _Boom:
        async def get(self, *a, **k):
            raise RuntimeError("network down")
    got = await find_social_handles("https://acme.com", client=_Boom())
    assert got == {"instagram": "", "youtube": "", "facebook": "", "linkedin": ""}


@pytest.mark.asyncio
async def test_happy_path_uses_client():
    class _Resp:
        status_code = 200
        text = _HTML

    class _Client:
        async def get(self, *a, **k):
            return _Resp()
    got = await find_social_handles("https://acme.com", client=_Client())
    assert "instagram.com/acmecandles" in got["instagram"]
