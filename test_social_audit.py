from scrapers.social.base import SocialProfile
from analyzer.social_audit import audit_profile, SocialIssue


def _healthy(**over):
    base = dict(
        platform="instagram", handle="acme", url="u",
        bio="We make artisan candles for calm homes. Shop link below.",
        followers=5000, following=800, posts_count=120,
        posts_last_30_days=12, posting_frequency="2-3x per week",
        avg_engagement_rate=2.1, uses_video=True, has_link_in_bio=True,
        last_post_age_days=3,
    )
    base.update(over)
    return SocialProfile(**base)


def test_healthy_profile_has_no_issues():
    assert audit_profile(_healthy()) == []


def test_unanalyzed_profile_yields_nothing():
    p = _healthy(analyzed=False, note="login wall")
    assert audit_profile(p) == []


def test_inactive_account_is_high_severity():
    issues = audit_profile(_healthy(posts_last_30_days=0, last_post_age_days=90))
    assert any(i.severity == "high" and "inactiv" in i.label.lower() for i in issues)


def test_low_engagement_flagged_for_instagram_not_youtube():
    ig = audit_profile(_healthy(avg_engagement_rate=0.2, followers=3000))
    assert any("engage" in i.label.lower() for i in ig)
    yt = audit_profile(_healthy(platform="youtube", avg_engagement_rate=0.2, followers=3000))
    assert not any("engage" in i.label.lower() for i in yt)


def test_no_video_flagged():
    issues = audit_profile(_healthy(uses_video=False))
    assert any("video" in i.label.lower() or "reel" in i.label.lower() for i in issues)


def test_no_link_in_bio_instagram_only():
    ig = audit_profile(_healthy(has_link_in_bio=False))
    assert any("link" in i.label.lower() for i in ig)
    li = audit_profile(_healthy(platform="linkedin", has_link_in_bio=False))
    assert not any("link" in i.label.lower() for i in li)


def test_weak_bio_flagged():
    issues = audit_profile(_healthy(bio="candles"))
    assert any("bio" in i.label.lower() for i in issues)


def test_follower_following_imbalance_instagram_small_accounts():
    issues = audit_profile(_healthy(followers=900, following=1500))
    assert any("follow" in i.label.lower() for i in issues)


def test_thin_catalogue_flagged():
    issues = audit_profile(_healthy(posts_count=4))
    assert any("thin" in i.label.lower() or "few post" in i.detail.lower() for i in issues)


def test_ranked_high_before_low():
    issues = audit_profile(_healthy(
        posts_last_30_days=0, last_post_age_days=90,  # high
        bio="candles",                                # low
    ))
    sevs = [i.severity for i in issues]
    assert sevs == sorted(sevs, key=lambda s: {"high": 0, "medium": 1, "low": 2}[s])


def test_detail_has_no_dashes():
    issues = audit_profile(_healthy(uses_video=False, has_link_in_bio=False, bio="x"))
    for i in issues:
        assert "-" not in i.detail and "—" not in i.detail and "–" not in i.detail
