from scrapers.social.base import SocialProfile, classify_frequency, unanalyzed


def test_socialprofile_defaults():
    p = SocialProfile(platform="instagram", handle="acme", url="https://instagram.com/acme")
    assert p.followers == 0
    assert p.analyzed is True
    assert p.note == ""
    assert p.sample_captions == []
    assert p.last_post_age_days is None


def test_socialprofile_sample_captions_not_shared():
    a = SocialProfile(platform="x", handle="a", url="u")
    b = SocialProfile(platform="x", handle="b", url="u")
    a.sample_captions.append("hi")
    assert b.sample_captions == []


def test_classify_frequency_bands():
    assert classify_frequency(25) == "daily"
    assert classify_frequency(20) == "daily"
    assert classify_frequency(10) == "2-3x per week"
    assert classify_frequency(8) == "2-3x per week"
    assert classify_frequency(5) == "weekly"
    assert classify_frequency(4) == "weekly"
    assert classify_frequency(2) == "irregular"
    assert classify_frequency(1) == "irregular"
    assert classify_frequency(0) == "inactive (no posts in 30 days)"


def test_unanalyzed_helper():
    p = unanalyzed("facebook", "acmepage", "https://facebook.com/acmepage", "login wall")
    assert p.analyzed is False
    assert p.note == "login wall"
    assert p.platform == "facebook"
    assert p.followers == 0
