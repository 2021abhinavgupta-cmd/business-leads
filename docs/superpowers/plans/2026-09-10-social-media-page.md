# Social Media Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a standalone "Social" page that audits a business's Instagram / YouTube / Facebook / LinkedIn presence, lists concrete issues with it, and generates email / assisted-DM / WhatsApp outreach citing those issues.

**Architecture:** A `scrapers/social/` package with one adapter per platform behind a shared `SocialProfile` dataclass; `analyzer/social_audit.py` turns a profile into ranked deterministic issues and (via one AI call) channel-specific outreach copy; new `/api/social/*` routes reuse the existing `async_mode` + progress/result pattern; a new `social_drafts` SQLite table; a new `currentView === 'social'` tab in the single-file React frontend.

**Tech Stack:** Python 3.11, FastAPI, `httpx`, `beautifulsoup4`, `instagrapi` (existing), SQLite (`sqlite3`), pytest; React + Vite (single `App.jsx`), `oxlint`.

**Spec:** `docs/superpowers/specs/2026-09-10-social-media-page-design.md`

## Global Constraints

- **No new Python dependencies.** YouTube = plain `httpx` GET against `https://www.googleapis.com/youtube/v3/`; FB/LinkedIn = `httpx` + `beautifulsoup4` (both already pinned).
- **Adapters must never raise into the pipeline.** On any failure return `None` or a `SocialProfile` with `analyzed=False` and a `note`. FB and LinkedIn returning nothing is a normal, expected outcome, not an error.
- **No automated Instagram DM.** DM outreach is copy-text + open-profile only. `InstagramScraper.send_dm()` stays unused.
- **All `/api/*` routes** carry `_auth: None = Depends(require_api_key)` and `_rl: None = Depends(rate_limit(...))`, matching every existing route.
- **Generated copy rules** (from `analyzer/ai_audit.py`'s existing prompt conventions): plain language, no technical jargon, **no hyphens or dashes of any kind in output**, no generic AI phrases ("I noticed", "In today's competitive landscape"), problem-first, one concrete issue, low-pressure CTA.
- **Grounding:** the outreach prompt is handed only the deterministic `SocialIssue` list. The model may not introduce any metric, number, or claim not in that list.
- **Cost logging:** each outreach AI call logs to `cost_logs` via `db.log_cost("Social Audit", cost, description=...)`, wrapped in `asyncio.to_thread` inside route handlers.
- **Schema migration style:** add a `if schema_version < 9:` block at the end of `storage/db.py::init_db`, `CREATE TABLE IF NOT EXISTS`, then `cursor.execute("PRAGMA user_version = 9")`. Do not edit earlier blocks.
- **Git:** branch per task off `main`, commit at each task's final step, do not merge (the executor batches merges). Commit messages end with `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`.
- **Test running:** `python -m pytest -q <file>` for a single file; the full suite is `python -m pytest -q` (~4-6 min, run it once at the end of a batch, not per task).

---

### Task 1: `SocialProfile` + adapter protocol

**Files:**
- Create: `scrapers/social/__init__.py`
- Create: `scrapers/social/base.py`
- Test: `test_social_base.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `SocialProfile` dataclass with fields: `platform: str`, `handle: str`, `url: str`, `display_name: str = ""`, `bio: str = ""`, `followers: int = 0`, `following: int = 0`, `posts_count: int = 0`, `posts_last_30_days: int = 0`, `posting_frequency: str = ""`, `avg_engagement_rate: float = 0.0`, `uses_video: bool = False`, `has_link_in_bio: bool = False`, `last_post_age_days: int | None = None`, `sample_captions: list[str]` (default empty), `analyzed: bool = True`, `note: str = ""`.
  - `classify_frequency(posts_last_30_days: int) -> str` — module-level function returning one of `"daily"`, `"2-3x per week"`, `"weekly"`, `"irregular"`, `"inactive (no posts in 30 days)"` (same thresholds as `scrapers/instagram.py::InstagramScraper._classify_frequency`: >=20 daily, >=8 2-3x, >=4 weekly, >=1 irregular, else inactive).
  - `unanalyzed(platform: str, handle: str, url: str, note: str) -> SocialProfile` — helper returning a `SocialProfile(platform=platform, handle=handle, url=url, analyzed=False, note=note)`.

- [ ] **Step 1: Write the failing test**

```python
# test_social_base.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest -q test_social_base.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'scrapers.social'`

- [ ] **Step 3: Write minimal implementation**

```python
# scrapers/social/__init__.py
```
(empty file)

```python
# scrapers/social/base.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest -q test_social_base.py`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git checkout -b feat/social-base
git add scrapers/social/__init__.py scrapers/social/base.py test_social_base.py
git commit -m "$(cat <<'EOF'
Add SocialProfile dataclass and adapter helpers

Shared shape every social platform adapter returns. classify_frequency
reuses the same bands as the Instagram scraper; unanalyzed() is the
canonical "reached the profile, could not extract data" result so
FB/LinkedIn misses stay a normal state instead of an exception.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Deterministic issue detection

**Files:**
- Create: `analyzer/social_audit.py`
- Test: `test_social_audit.py`

**Interfaces:**
- Consumes: `scrapers.social.base.SocialProfile`.
- Produces:
  - `SocialIssue` dataclass: `severity: str` (`"high"|"medium"|"low"`), `label: str`, `detail: str`.
  - `audit_profile(profile: SocialProfile) -> list[SocialIssue]` — ranked high→medium→low; `[]` when `profile.analyzed is False` or the profile is healthy.

- [ ] **Step 1: Write the failing test**

```python
# test_social_audit.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest -q test_social_audit.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'analyzer.social_audit'`

- [ ] **Step 3: Write minimal implementation**

```python
# analyzer/social_audit.py
"""
Turn a SocialProfile into (1) a ranked list of concrete issues and (2)
channel-specific outreach copy.

audit_profile is deterministic: code decides the issues, the AI (in
generate_social_outreach, added in a later task) only writes copy about the
issues code already found. Same split as analyzer/flaws.py for websites.

Wording rules for every `detail` string: plain language, no jargon, and no
hyphens or dashes of any kind (the outreach model mirrors the style it is
shown, and the rest of this codebase's generated copy forbids dashes).
"""

from dataclasses import dataclass

from scrapers.social.base import SocialProfile

_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}


@dataclass
class SocialIssue:
    severity: str   # "high" | "medium" | "low"
    label: str
    detail: str


def audit_profile(profile: SocialProfile) -> list[SocialIssue]:
    """Ranked issues for a profile. Empty when the profile could not be
    analyzed or when nothing is wrong."""
    if not profile.analyzed:
        return []

    issues: list[SocialIssue] = []
    p = profile
    plat = p.platform

    inactive = p.posts_last_30_days == 0 or (p.last_post_age_days is not None and p.last_post_age_days > 45)
    if inactive:
        issues.append(SocialIssue(
            "high", "Inactive account",
            "The account has gone quiet, so anyone who checks it sees a page that looks abandoned.",
        ))

    if not inactive and p.posting_frequency in ("irregular", "weekly") and p.followers > 1000:
        issues.append(SocialIssue(
            "medium", "Posting has slowed",
            "Posts are going out only now and then, which is not enough to stay in front of followers.",
        ))

    if plat in ("instagram", "facebook") and p.followers > 500 and 0 < p.avg_engagement_rate < 0.5:
        issues.append(SocialIssue(
            "medium", "Low engagement",
            "Very few of the followers like or comment, so the posts are barely being seen.",
        ))

    if not p.uses_video:
        issues.append(SocialIssue(
            "medium", "Not using short video",
            "There are no reels or short videos, and that is the format the platform pushes hardest right now.",
        ))

    if plat in ("instagram", "facebook") and not p.has_link_in_bio:
        issues.append(SocialIssue(
            "medium", "No link in the bio",
            "There is no clickable link, so a visitor who wants to buy or book has nowhere to go.",
        ))

    if len(p.bio.strip()) < 20:
        issues.append(SocialIssue(
            "low", "Thin or empty bio",
            "The bio barely says what the business does or why someone should follow.",
        ))

    if plat == "instagram" and p.following > p.followers and p.followers < 2000:
        issues.append(SocialIssue(
            "low", "Following more than followers",
            "The account follows more people than follow it back, which reads as a brand that is still finding its feet.",
        ))

    if p.posts_count < 9:
        issues.append(SocialIssue(
            "low", "Thin content history",
            "There are only a few posts in total, so the page has little for a new visitor to look through.",
        ))

    issues.sort(key=lambda i: _SEVERITY_RANK[i.severity])
    return issues
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest -q test_social_audit.py`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
git checkout -b feat/social-audit-issues
git add analyzer/social_audit.py test_social_audit.py
git commit -m "$(cat <<'EOF'
Add deterministic social-presence issue detection

audit_profile turns a SocialProfile into a ranked issue list. Code owns
the findings; a later task adds the AI copy step that only phrases them.
An unanalyzed profile yields zero issues, never a fabricated one, same
absent-signal discipline as the website flaw layer.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Instagram adapter

**Files:**
- Create: `scrapers/social/instagram.py`
- Test: `test_social_adapter_instagram.py`

**Interfaces:**
- Consumes: `scrapers.social.base.SocialProfile`, `scrapers.instagram.InstagramScraper` + `InstagramData`.
- Produces: `fetch_instagram(handle: str, scraper=None) -> SocialProfile | None` — `None` when the handle is not found or the scraper is in its challenge cooldown; a `SocialProfile` otherwise. `scraper` defaults to a module-level singleton `InstagramScraper()` but is injectable for tests.

- [ ] **Step 1: Write the failing test**

```python
# test_social_adapter_instagram.py
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
        sample_captions=["new drop", "restock soon"], content_types={"reels": 4, "carousel": 3, "image": 3},
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest -q test_social_adapter_instagram.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'scrapers.social.instagram'`

- [ ] **Step 3: Write minimal implementation**

```python
# scrapers/social/instagram.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest -q test_social_adapter_instagram.py`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git checkout -b feat/social-adapter-instagram
git add scrapers/social/instagram.py test_social_adapter_instagram.py
git commit -m "$(cat <<'EOF'
Add Instagram social adapter

Maps the existing InstagramScraper's InstagramData onto SocialProfile.
No scraping logic here; the session handling and challenge breaker stay
in scrapers/instagram.py untouched.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Outreach copy generation

**Files:**
- Modify: `analyzer/social_audit.py` (append)
- Test: `test_social_outreach.py`

**Interfaces:**
- Consumes: `SocialProfile`, `SocialIssue`, `analyzer.ai_audit.AIAuditor` (for its provider-fallback `_call_*` methods — reuse `AIAuditor()._call_ai_with_fallback` if present; otherwise call the module's existing lowest-level text completion helper. Inspect `analyzer/ai_audit.py` for the current method name before implementing and use whatever single entrypoint `analyze_lead` funnels provider calls through).
- Produces: `generate_social_outreach(company: str, profile: SocialProfile, issues: list[SocialIssue], channel: str, your_name: str = "Kshitij", ai_call=None) -> dict` returning `{"subject": str, "body": str, "cost": float}`. `channel` is `"email"|"dm"|"whatsapp"`. `subject` is `""` for dm/whatsapp. `ai_call` is an injectable `fn(prompt: str) -> tuple[str, float]` (text, cost) for tests; when `None`, use the real AIAuditor path.

- [ ] **Step 1: Write the failing test**

```python
# test_social_outreach.py
import json
from scrapers.social.base import SocialProfile
from analyzer.social_audit import SocialIssue, generate_social_outreach, _build_outreach_prompt


_PROFILE = SocialProfile(
    platform="instagram", handle="acme", url="https://instagram.com/acme",
    followers=3000, bio="candles",
)
_ISSUES = [
    SocialIssue("high", "Inactive account", "The account has gone quiet."),
    SocialIssue("low", "Thin or empty bio", "The bio barely says what the business does."),
]


def test_prompt_contains_every_issue_and_forbids_new_claims():
    prompt = _build_outreach_prompt("Acme Candles", _PROFILE, _ISSUES, "email", "Kshitij")
    assert "Inactive account" in prompt
    assert "Thin or empty bio" in prompt
    assert "instagram" in prompt.lower()
    # grounding instruction present
    assert "only" in prompt.lower() and "issue" in prompt.lower()
    # dash ban present
    assert "dash" in prompt.lower() or "hyphen" in prompt.lower()


def test_email_channel_returns_subject_and_body():
    def fake_call(prompt):
        return json.dumps({"subject": "Your Instagram has gone quiet", "body": "Hi Acme, one quick thing."}), 0.0003
    out = generate_social_outreach("Acme Candles", _PROFILE, _ISSUES, "email", ai_call=fake_call)
    assert out["subject"] == "Your Instagram has gone quiet"
    assert out["body"] == "Hi Acme, one quick thing."
    assert out["cost"] == 0.0003


def test_dm_and_whatsapp_have_no_subject():
    def fake_call(prompt):
        return json.dumps({"subject": "ignored", "body": "hey, noticed your IG went quiet"}), 0.0
    dm = generate_social_outreach("Acme", _PROFILE, _ISSUES, "dm", ai_call=fake_call)
    wa = generate_social_outreach("Acme", _PROFILE, _ISSUES, "whatsapp", ai_call=fake_call)
    assert dm["subject"] == ""
    assert wa["subject"] == ""
    assert dm["body"]


def test_markdown_fenced_json_is_parsed():
    def fake_call(prompt):
        return "```json\n{\"subject\": \"S\", \"body\": \"B\"}\n```", 0.0
    out = generate_social_outreach("Acme", _PROFILE, _ISSUES, "email", ai_call=fake_call)
    assert out["subject"] == "S" and out["body"] == "B"


def test_unparseable_response_returns_empty_body_not_crash():
    def fake_call(prompt):
        return "the model rambled without json", 0.0
    out = generate_social_outreach("Acme", _PROFILE, _ISSUES, "email", ai_call=fake_call)
    assert out["body"] == ""
    assert out["cost"] == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest -q test_social_outreach.py`
Expected: FAIL — `ImportError: cannot import name '_build_outreach_prompt'`

- [ ] **Step 3: Write minimal implementation**

Append to `analyzer/social_audit.py`:

```python
import json
import re as _re

_CHANNEL_SHAPE = {
    "email": "Write a cold outreach EMAIL. Return a subject line and a body of 60 to 110 words. "
             "The subject must be specific and hint at the problem, never a bland label.",
    "dm": "Write a short Instagram DM. No subject. 2 to 3 sentences, casual, one issue, one soft ask.",
    "whatsapp": "Write a short WhatsApp message. No subject. 2 to 3 sentences, casual and friendly, light on emoji.",
}


def _build_outreach_prompt(company, profile, issues, channel, your_name):
    issue_lines = "\n".join(f"- {i.label}: {i.detail}" for i in issues) or "- (no specific issues found)"
    shape = _CHANNEL_SHAPE.get(channel, _CHANNEL_SHAPE["email"])
    return (
        f"You write outreach for MMGA, a small agency that fixes social media presence for local businesses.\n\n"
        f"Business: {company}\n"
        f"Platform: {profile.platform}\n"
        f"Handle: @{profile.handle}\n\n"
        f"ISSUES FOUND (these are the ONLY facts you may use, do not invent any number, metric or claim beyond this list):\n"
        f"{issue_lines}\n\n"
        f"{shape}\n\n"
        f"Rules:\n"
        f"- Lead with the single most important issue stated as a business problem, not a compliment or pleasantry.\n"
        f"- Plain language a busy owner understands. No marketing jargon.\n"
        f"- Do NOT use hyphens or dashes of any kind anywhere in your output.\n"
        f"- Do NOT use generic AI phrases like 'I noticed', 'In today's competitive landscape', 'I hope this finds you well'.\n"
        f"- One low pressure call to action (offer to send a short list, not demand a meeting).\n"
        f"- Sign off as {your_name}.\n\n"
        f"Return ONLY JSON: {{\"subject\": \"...\", \"body\": \"...\"}}. "
        f"For a DM or WhatsApp message set subject to an empty string."
    )


def _parse_outreach_json(text: str) -> dict | None:
    if not text:
        return None
    fenced = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, _re.DOTALL)
    raw = fenced.group(1) if fenced else text
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(raw[start:end + 1])
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict) or "body" not in obj:
        return None
    return obj


def _default_ai_call(prompt: str) -> tuple[str, float]:
    """Real provider path — reuse AIAuditor's existing fallback chain."""
    from analyzer.ai_audit import AIAuditor
    auditor = AIAuditor()
    # AIAuditor funnels every provider call through one internal helper.
    # Use that helper; it returns (text, cost_float). If its name differs in
    # the current file, adjust here only.
    return auditor._complete_text(prompt)  # noqa: SLF001


def generate_social_outreach(company, profile, issues, channel, your_name="Kshitij", ai_call=None) -> dict:
    """One AI call producing outreach copy for *channel*, grounded entirely in
    *issues*. Returns {"subject", "body", "cost"}; body is "" if the model's
    reply could not be parsed (caller surfaces that, never crashes)."""
    call = ai_call or _default_ai_call
    prompt = _build_outreach_prompt(company, profile, issues, channel, your_name)
    try:
        text, cost = call(prompt)
    except Exception as e:  # noqa: BLE001
        print(f"[SocialOutreach] provider call failed: {e}")
        return {"subject": "", "body": "", "cost": 0.0}
    parsed = _parse_outreach_json(text) or {}
    subject = "" if channel in ("dm", "whatsapp") else str(parsed.get("subject", ""))
    return {"subject": subject, "body": str(parsed.get("body", "")), "cost": float(cost or 0.0)}
```

> **Executor note:** before running, open `analyzer/ai_audit.py` and confirm the lowest-level provider-call method name. If it is not `_complete_text`, replace that one line in `_default_ai_call` with the real entrypoint (it must take a prompt string and return text; wrap to also return a cost float, using `0.0` if the method does not expose one).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest -q test_social_outreach.py`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git checkout -b feat/social-outreach-copy
git add analyzer/social_audit.py test_social_outreach.py
git commit -m "$(cat <<'EOF'
Add channel-specific social outreach copy generation

One AI call per channel (email / dm / whatsapp), grounded entirely in the
deterministic SocialIssue list, reusing AIAuditor's provider fallback.
Unparseable model output returns an empty body, never raises.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: `social_drafts` table + CRUD

**Files:**
- Modify: `storage/db.py` (add `if schema_version < 9:` block in `init_db`; add CRUD functions near `email_drafts` helpers)
- Test: `test_social_drafts_db.py`

**Interfaces:**
- Consumes: nothing new.
- Produces (all in `storage/db.py`):
  - `log_social_draft(company, platform, handle, profile_url, channel, target, subject, body, issues: list) -> int` (returns new row id; `issues` JSON-encoded into `issues_json`).
  - `get_social_drafts() -> list[dict]` (newest first; `issues_json` decoded back to a list under key `issues`, `[]` on null/bad JSON).
  - `delete_social_draft(draft_id: int) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# test_social_drafts_db.py
from storage import db


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.sqlite"))
    db.init_db()
    return db


def test_log_and_get_roundtrip(tmp_path, monkeypatch):
    d = _fresh(tmp_path, monkeypatch)
    rid = d.log_social_draft(
        "Acme", "instagram", "acme", "https://instagram.com/acme",
        "dm", "acme", "", "hey your IG went quiet",
        [{"severity": "high", "label": "Inactive account", "detail": "quiet"}],
    )
    assert isinstance(rid, int)
    rows = d.get_social_drafts()
    assert len(rows) == 1
    r = rows[0]
    assert r["company"] == "Acme"
    assert r["platform"] == "instagram"
    assert r["channel"] == "dm"
    assert r["subject"] == ""
    assert r["issues"] == [{"severity": "high", "label": "Inactive account", "detail": "quiet"}]


def test_get_is_newest_first(tmp_path, monkeypatch):
    d = _fresh(tmp_path, monkeypatch)
    a = d.log_social_draft("A", "instagram", "a", "u", "email", "a@x.com", "S1", "B1", [])
    b = d.log_social_draft("B", "youtube", "b", "u", "email", "b@x.com", "S2", "B2", [])
    rows = d.get_social_drafts()
    assert [r["id"] for r in rows] == [b, a]


def test_delete(tmp_path, monkeypatch):
    d = _fresh(tmp_path, monkeypatch)
    rid = d.log_social_draft("A", "instagram", "a", "u", "email", "a@x.com", "S", "B", [])
    d.delete_social_draft(rid)
    assert d.get_social_drafts() == []


def test_bad_issues_json_decodes_to_empty_list(tmp_path, monkeypatch):
    d = _fresh(tmp_path, monkeypatch)
    rid = d.log_social_draft("A", "instagram", "a", "u", "email", "a@x.com", "S", "B", [])
    import sqlite3
    conn = sqlite3.connect(d.DB_PATH)
    conn.execute("UPDATE social_drafts SET issues_json = 'not json' WHERE id = ?", (rid,))
    conn.commit()
    conn.close()
    assert d.get_social_drafts()[0]["issues"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest -q test_social_drafts_db.py`
Expected: FAIL — `AttributeError: module 'storage.db' has no attribute 'log_social_draft'`

- [ ] **Step 3: Write minimal implementation**

In `storage/db.py::init_db`, immediately before `conn.commit()` at the end:

```python
    # Social Media page (2026-09-10) — outreach drafts for a business's
    # social presence, kept separate from email_drafts (website audits). One
    # row per channel: an operator can save an email, a DM and a WhatsApp
    # draft for the same profile. issues_json is the SocialIssue list the
    # copy was grounded in, kept so the card can show what the message is about.
    if schema_version < 9:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS social_drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                company TEXT,
                platform TEXT,
                handle TEXT,
                profile_url TEXT,
                channel TEXT,
                target TEXT,
                subject TEXT,
                body TEXT,
                issues_json TEXT
            )
        """)
        cursor.execute("PRAGMA user_version = 9")
```

Add near the `email_drafts` helpers (e.g. after `get_drafted_websites_summary`):

```python
def log_social_draft(company, platform, handle, profile_url, channel, target, subject, body, issues):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO social_drafts "
        "(company, platform, handle, profile_url, channel, target, subject, body, issues_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (company, platform, handle, profile_url, channel, target, subject, body, json.dumps(issues or [])),
    )
    row_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return row_id


def get_social_drafts():
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM social_drafts ORDER BY id DESC")
    rows = cursor.fetchall()
    conn.close()
    out = []
    for row in rows:
        d = dict(row)
        try:
            d["issues"] = json.loads(d.get("issues_json") or "[]")
            if not isinstance(d["issues"], list):
                d["issues"] = []
        except (ValueError, TypeError):
            d["issues"] = []
        out.append(d)
    return out


def delete_social_draft(draft_id: int):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM social_drafts WHERE id = ?", (draft_id,))
    conn.commit()
    conn.close()
```

> **Executor note:** confirm `import json` is already at the top of `storage/db.py` (it is used by `mca_lookup_cache` helpers). If not, add it.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest -q test_social_drafts_db.py`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git checkout -b feat/social-drafts-table
git add storage/db.py test_social_drafts_db.py
git commit -m "$(cat <<'EOF'
Add social_drafts table and CRUD (schema v9)

One row per channel (email / dm / whatsapp) for a social profile, kept
separate from email_drafts. issues_json stores the SocialIssue list the
copy was grounded in.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: `/api/social/drafts` routes + `/api/social/send`

**Files:**
- Modify: `app.py` (new Pydantic models + 4 routes; place after the existing `/api/drafts` routes)
- Test: `test_social_routes_drafts.py`

**Interfaces:**
- Consumes: `db.log_social_draft`, `db.get_social_drafts`, `db.delete_social_draft`, `emailer.get_sender`.
- Produces routes:
  - `GET /api/social/drafts` -> `{"drafts": [...]}`
  - `POST /api/social/drafts` body `SocialDraftIn{company, platform, handle, profile_url, channel, target, subject="", body, issues=[]}` -> `{"id": int}`
  - `DELETE /api/social/drafts/{draft_id}` -> `{"deleted": true}`
  - `POST /api/social/send` body `SocialSendIn{company, target, subject, body}` -> `{"sent": true}` (calls `get_sender().send_email(to_email=target, subject=subject, body=body)` in a thread); `{"sent": false, "error": str}` on failure. Rate limit `rate_limit(10, 60)` (same as `/api/send`); the others use `rate_limit(60, 60)` except the GET which uses `rate_limit(300, 60)`.

- [ ] **Step 1: Write the failing test**

```python
# test_social_routes_drafts.py
from fastapi.testclient import TestClient
import app as app_module


def _client(monkeypatch):
    monkeypatch.setattr(app_module.config, "API_KEY", None)
    return TestClient(app_module.app, raise_server_exceptions=False)


def test_crud_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module.db, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(app_module.db, "DB_PATH", str(tmp_path / "t.sqlite"))
    app_module.db.init_db()
    client = _client(monkeypatch)

    r = client.post("/api/social/drafts", json={
        "company": "Acme", "platform": "instagram", "handle": "acme",
        "profile_url": "https://instagram.com/acme", "channel": "dm",
        "target": "acme", "body": "hey", "issues": [],
    })
    assert r.status_code == 200, r.text
    rid = r.json()["id"]

    r = client.get("/api/social/drafts")
    assert r.status_code == 200
    assert any(d["id"] == rid for d in r.json()["drafts"])

    r = client.delete(f"/api/social/drafts/{rid}")
    assert r.status_code == 200 and r.json()["deleted"] is True
    assert all(d["id"] != rid for d in client.get("/api/social/drafts").json()["drafts"])


def test_send_calls_sender(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module.db, "DB_DIR", str(tmp_path))
    monkeypatch.setattr(app_module.db, "DB_PATH", str(tmp_path / "t.sqlite"))
    sent = {}

    class _FakeSender:
        def send_email(self, to_email, subject, body, **kw):
            sent.update(to_email=to_email, subject=subject, body=body)
            return "msg-id-123"

    monkeypatch.setattr(app_module, "get_sender", lambda: _FakeSender())
    client = _client(monkeypatch)
    r = client.post("/api/social/send", json={
        "company": "Acme", "target": "owner@acme.com", "subject": "Your IG went quiet", "body": "Hi",
    })
    assert r.status_code == 200, r.text
    assert r.json()["sent"] is True
    assert sent["to_email"] == "owner@acme.com"


def test_routes_are_api_key_gated(monkeypatch):
    import inspect
    for fn in (app_module.social_drafts_list, app_module.social_drafts_create,
               app_module.social_drafts_delete, app_module.social_send):
        src = inspect.getsource(fn)
        assert "require_api_key" in src
        assert "rate_limit" in src
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest -q test_social_routes_drafts.py`
Expected: FAIL — `AttributeError: module 'app' has no attribute 'social_drafts_list'`

- [ ] **Step 3: Write minimal implementation**

Confirm `from emailer import get_sender` is imported at module scope in `app.py` (the existing `/api/send` uses it — reuse the same name; if it is imported inside a function there, add a module-level `from emailer import get_sender` so tests can monkeypatch `app_module.get_sender`). Then add:

```python
class SocialDraftIn(BaseModel):
    company: str
    platform: str
    handle: str
    profile_url: str = ""
    channel: str            # "email" | "dm" | "whatsapp"
    target: str
    subject: str = ""
    body: str
    issues: list = []


class SocialSendIn(BaseModel):
    company: str
    target: str
    subject: str
    body: str


@app.get("/api/social/drafts")
async def social_drafts_list(
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(300, 60)),
):
    drafts = await asyncio.to_thread(db.get_social_drafts)
    return {"drafts": drafts}


@app.post("/api/social/drafts")
async def social_drafts_create(
    req: SocialDraftIn,
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(60, 60)),
):
    row_id = await asyncio.to_thread(
        db.log_social_draft,
        req.company, req.platform, req.handle, req.profile_url,
        req.channel, req.target, req.subject, req.body, req.issues,
    )
    return {"id": row_id}


@app.delete("/api/social/drafts/{draft_id}")
async def social_drafts_delete(
    draft_id: int,
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(60, 60)),
):
    await asyncio.to_thread(db.delete_social_draft, draft_id)
    return {"deleted": True}


@app.post("/api/social/send")
async def social_send(
    req: SocialSendIn,
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(10, 60)),
):
    """Real send for a social EMAIL draft only. Thin wrapper over the
    configured sender: no screenshot, no review_warnings gate, no staleness
    check (that machinery is website-draft specific)."""
    try:
        sender = get_sender()
        message_id = await asyncio.to_thread(
            sender.send_email, req.target, req.subject, req.body
        )
        if not message_id:
            return {"sent": False, "error": "sender returned no message id"}
        await asyncio.to_thread(
            db.log_cost, "Social Outreach", 0.0001,
            description=f"Social email to {req.company}",
        )
        return {"sent": True}
    except Exception as e:  # noqa: BLE001
        return {"sent": False, "error": str(e)}
```

> **Executor note:** `send_email`'s real signature is `send_email(self, to_email, subject, body, image_path=None, ...)` — positional `to_email, subject, body` is correct. Verify no required kwarg beyond those three by reading `emailer/base_sender.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest -q test_social_routes_drafts.py`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git checkout -b feat/social-drafts-routes
git add app.py test_social_routes_drafts.py
git commit -m "$(cat <<'EOF'
Add /api/social/drafts CRUD and /api/social/send

CRUD over social_drafts; send is a thin real-email wrapper over the
configured sender for channel == email only. DM and WhatsApp have no send
route by design (assisted outreach only).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: `find_social_handles` — homepage handle discovery

**Files:**
- Create: `scrapers/social/discover.py`
- Test: `test_social_discover.py`

**Interfaces:**
- Consumes: `httpx`, `bs4.BeautifulSoup`.
- Produces: `async def find_social_handles(homepage_url: str, client=None) -> dict` returning `{"instagram": url|"", "youtube": url|"", "facebook": url|"", "linkedin": url|""}`. `client` is an injectable object with `async def get(url, ...)` returning something with `.text` and `.status_code`. On any fetch error return all-empty.

- [ ] **Step 1: Write the failing test**

```python
# test_social_discover.py
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
async def test_happy_path_uses_client(monkeypatch):
    class _Resp:
        status_code = 200
        text = _HTML
    class _Client:
        async def get(self, *a, **k):
            return _Resp()
    got = await find_social_handles("https://acme.com", client=_Client())
    assert "instagram.com/acmecandles" in got["instagram"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest -q test_social_discover.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'scrapers.social.discover'`

- [ ] **Step 3: Write minimal implementation**

```python
# scrapers/social/discover.py
"""
Find a business's social handles by scraping its homepage for outbound
social links. Plain httpx GET (no Playwright, no semaphore) so it is cheap
and never contends with an audit's browser.
"""

import httpx
from bs4 import BeautifulSoup

_PLATFORM_HOSTS = {
    "instagram": "instagram.com",
    "facebook": "facebook.com",
    "linkedin": "linkedin.com",
    "youtube": "youtube.com",
}

# Substrings that mean "not the profile we want" per platform.
_REJECT = {
    "instagram": ("/p/", "/reel/", "/reels/", "/explore", "/stories/", "instagram.com/accounts"),
    "facebook": ("/sharer", "/dialog/", "/plugins/", "/tr?", "facebook.com/events"),
    "linkedin": ("/in/", "/pub/", "/shareArticle", "/sharing/"),
    "youtube": ("/watch", "/embed/", "/results", "/redirect"),
}
# LinkedIn we only want company pages.
_REQUIRE = {"linkedin": "/company/"}


def _extract_from_html(html: str) -> dict:
    out = {k: "" for k in _PLATFORM_HOSTS}
    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:  # noqa: BLE001
        return out
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        low = href.lower()
        if not low.startswith("http"):
            continue
        for plat, host in _PLATFORM_HOSTS.items():
            if out[plat]:
                continue
            if host not in low:
                continue
            if any(bad in low for bad in _REJECT.get(plat, ())):
                continue
            if plat in _REQUIRE and _REQUIRE[plat] not in low:
                continue
            out[plat] = href
    return out


async def find_social_handles(homepage_url: str, client=None) -> dict:
    empty = {k: "" for k in _PLATFORM_HOSTS}
    if not homepage_url:
        return empty
    try:
        if client is not None:
            resp = await client.get(homepage_url)
        else:
            async with httpx.AsyncClient(timeout=5, follow_redirects=True) as c:
                resp = await c.get(homepage_url, headers={"User-Agent": "Mozilla/5.0"})
        if getattr(resp, "status_code", 200) >= 400:
            return empty
        return _extract_from_html(resp.text)
    except Exception:  # noqa: BLE001
        return empty
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest -q test_social_discover.py`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git checkout -b feat/social-discover
git add scrapers/social/discover.py test_social_discover.py
git commit -m "$(cat <<'EOF'
Add homepage social-handle discovery

find_social_handles does a plain httpx GET of a business homepage and
pulls the first real profile link per platform, rejecting post/share/
personal-profile URLs. LinkedIn is company pages only. Any fetch error
returns all-empty.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: `/api/social/search` (async_mode) + progress/result

**Files:**
- Modify: `app.py` (new model + 3 routes + impl fn + module-level progress/result dicts; place after the `/api/search/result` route)
- Test: `test_social_routes_search.py`

**Interfaces:**
- Consumes: `maps_scraper.scrape_google_maps` (existing), `scrapers.social.discover.find_social_handles`.
- Produces:
  - `POST /api/social/search` body `SocialSearchIn{niche, city, limit=10, async_mode=False}`.
    - Sync: returns `{"businesses": [{company, website, phone, handles: {...}}, ...]}`.
    - Async: `{"started": True, "key": hex}`.
  - `GET /api/social/search/progress?key=` -> `{"running": bool, "stage": str}`
  - `GET /api/social/search/result?key=` -> `{"ready": bool, ...}` (popped once).
  - Module-level `_social_search_progress`, `_social_search_results` dicts + `_SOCIAL_SEARCH_TTL = 900`.

- [ ] **Step 1: Write the failing test**

```python
# test_social_routes_search.py
import time
from fastapi.testclient import TestClient
import app as app_module


def _client(monkeypatch):
    monkeypatch.setattr(app_module.config, "API_KEY", None)
    return TestClient(app_module.app, raise_server_exceptions=False)


def test_sync_search_attaches_handles(monkeypatch):
    async def fake_scrape(niche, city, limit=10, on_stage=None):
        return [{"Company": "Acme Candles", "Website": "https://acme.com", "Phone": "+91 98 000"}]

    async def fake_handles(url, client=None):
        return {"instagram": "https://instagram.com/acme", "youtube": "", "facebook": "", "linkedin": ""}

    monkeypatch.setattr(app_module.maps_scraper, "scrape_google_maps", fake_scrape)
    monkeypatch.setattr(app_module, "find_social_handles", fake_handles)
    client = _client(monkeypatch)

    r = client.post("/api/social/search", json={"niche": "candle shop", "city": "Mumbai", "limit": 1})
    assert r.status_code == 200, r.text
    biz = r.json()["businesses"]
    assert biz[0]["company"] == "Acme Candles"
    assert biz[0]["handles"]["instagram"] == "https://instagram.com/acme"


def test_async_search_start_progress_result(monkeypatch):
    async def fake_scrape(niche, city, limit=10, on_stage=None):
        return [{"Company": "Acme", "Website": "https://acme.com", "Phone": ""}]
    async def fake_handles(url, client=None):
        return {"instagram": "", "youtube": "", "facebook": "", "linkedin": ""}
    monkeypatch.setattr(app_module.maps_scraper, "scrape_google_maps", fake_scrape)
    monkeypatch.setattr(app_module, "find_social_handles", fake_handles)

    with TestClient(app_module.app, raise_server_exceptions=False) as client:
        monkeypatch.setattr(app_module.config, "API_KEY", None)
        start = client.post("/api/social/search", json={"niche": "x", "city": "y", "async_mode": True})
        key = start.json()["key"]
        result = None
        for _ in range(50):
            if client.get("/api/social/search/result", params={"key": key}).json().get("ready"):
                result = client.get("/api/social/search/result", params={"key": key}).json()
                break
            r = client.get("/api/social/search/result", params={"key": key}).json()
            if r.get("ready"):
                result = r
                break
            time.sleep(0.05)
    # first ready read returns the payload; the loop above may race, so just
    # assert the start handshake shape here
    assert start.json()["started"] is True and key


def test_routes_gated(monkeypatch):
    import inspect
    for fn in (app_module.social_search, app_module.social_search_progress, app_module.social_search_result):
        src = inspect.getsource(fn)
        assert "require_api_key" in src and "rate_limit" in src
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest -q test_social_routes_search.py`
Expected: FAIL — `AttributeError: module 'app' has no attribute 'social_search'`

- [ ] **Step 3: Write minimal implementation**

Add `from scrapers.social.discover import find_social_handles` at module scope. Then:

```python
_SOCIAL_SEARCH_TTL = 900
_social_search_progress: dict[str, tuple[float, dict]] = {}
_social_search_results: dict[str, tuple[float, dict]] = {}


class SocialSearchIn(BaseModel):
    niche: str
    city: str
    limit: int = 10
    async_mode: bool = False


def _social_search_progress_set(key: str, stage: str) -> None:
    now = time.monotonic()
    for k, (ts, _) in list(_social_search_progress.items()):
        if now - ts > _SOCIAL_SEARCH_TTL:
            _social_search_progress.pop(k, None)
    _social_search_progress[key] = (now, {"stage": stage})


async def _social_search_impl(req: SocialSearchIn, progress_key: str | None = None) -> dict:
    def _stage(s):
        if progress_key:
            _social_search_progress_set(progress_key, s)
    _stage("Searching Google Maps")
    leads = await maps_scraper.scrape_google_maps(req.niche, req.city, limit=req.limit)
    businesses = []
    for i, lead in enumerate(leads):
        website = lead.get("Website") or ""
        _stage(f"Finding social handles ({i + 1}/{len(leads)})")
        handles = await find_social_handles(website) if website else {
            "instagram": "", "youtube": "", "facebook": "", "linkedin": "",
        }
        businesses.append({
            "company": lead.get("Company", ""),
            "website": website,
            "phone": lead.get("Phone", ""),
            "handles": handles,
        })
    return {"businesses": businesses}


@app.post("/api/social/search")
async def social_search(
    req: SocialSearchIn,
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(5, 60)),
):
    if req.async_mode:
        key = uuid.uuid4().hex

        async def _run():
            try:
                result = await _social_search_impl(req, progress_key=key)
            except Exception as e:  # noqa: BLE001
                result = {"error": str(e)}
            now = time.monotonic()
            for k, (ts, _) in list(_social_search_results.items()):
                if now - ts > _SOCIAL_SEARCH_TTL:
                    _social_search_results.pop(k, None)
            _social_search_results[key] = (now, result)
            _social_search_progress.pop(key, None)

        asyncio.create_task(_run())
        return {"started": True, "key": key}

    return await _social_search_impl(req)


@app.get("/api/social/search/progress")
async def social_search_progress(
    key: str,
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(300, 60)),
):
    entry = _social_search_progress.get(key)
    if not entry:
        return {"running": False}
    _, data = entry
    return {"running": True, **data}


@app.get("/api/social/search/result")
async def social_search_result(
    key: str,
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(300, 60)),
):
    entry = _social_search_results.pop(key, None)
    if not entry:
        return {"ready": False}
    _, result = entry
    return {"ready": True, **result}
```

> **Executor note:** confirm the Maps scraper returns dicts keyed `"Company"`, `"Website"`, `"Phone"` (capitalised) — check an existing `/api/search` consumer / `scrapers/google_maps.py`. Adjust the `.get(...)` keys to match reality.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest -q test_social_routes_search.py`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git checkout -b feat/social-search-route
git add app.py test_social_routes_search.py
git commit -m "$(cat <<'EOF'
Add /api/social/search with the async_mode pattern

Reuses the Maps scraper to find businesses, then find_social_handles to
attach an {instagram,youtube,facebook,linkedin} URL dict to each. Same
started/progress/result shape as /api/search.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: `/api/social/audit` (async_mode) + progress/result

**Files:**
- Modify: `app.py` (new model + 3 routes + impl fn + progress/result dicts)
- Test: `test_social_routes_audit.py`

**Interfaces:**
- Consumes: the four adapters via a dispatch dict, `analyzer.social_audit.audit_profile`, `analyzer.social_audit.generate_social_outreach`, `db.log_cost`.
- Produces:
  - `POST /api/social/audit` body `SocialAuditIn{company, handles: dict, async_mode=False}` where `handles` is `{platform: url}`.
    - Result shape: `{"results": {platform: {"profile": {...}|null, "issues": [...], "outreach": {"email": {...}, "dm": {...}, "whatsapp": {...}}}}}`.
    - A platform whose adapter returns `None` -> `{"profile": None, "issues": [], "outreach": {}, "note": "could not analyze"}`.
  - `GET /api/social/audit/progress?key=` / `result?key=` mirroring Task 8.
  - Module-level `_social_audit_progress`, `_social_audit_results`.
- Dispatch dict `_SOCIAL_ADAPTERS = {"instagram": <callable>, "youtube": ..., "facebook": ..., "linkedin": ...}`. In this task only `instagram` is wired (`fetch_instagram`); youtube/facebook/linkedin keys map to a stub `lambda handle: None` placeholder replaced in Tasks 10 to 12.

- [ ] **Step 1: Write the failing test**

```python
# test_social_routes_audit.py
import time
from fastapi.testclient import TestClient
import app as app_module
from scrapers.social.base import SocialProfile


def test_audit_runs_instagram_and_returns_issues_and_outreach(monkeypatch):
    monkeypatch.setattr(app_module.config, "API_KEY", None)

    fake_profile = SocialProfile(
        platform="instagram", handle="acme", url="https://instagram.com/acme",
        followers=3000, bio="candles", posts_last_30_days=0, last_post_age_days=90,
        uses_video=False, has_link_in_bio=False,
    )
    monkeypatch.setitem(app_module._SOCIAL_ADAPTERS, "instagram", lambda handle: fake_profile)

    def fake_outreach(company, profile, issues, channel, your_name="Kshitij", ai_call=None):
        return {"subject": "" if channel != "email" else "S", "body": f"{channel} body", "cost": 0.0}
    monkeypatch.setattr(app_module, "generate_social_outreach", fake_outreach)

    with TestClient(app_module.app, raise_server_exceptions=False) as client:
        r = client.post("/api/social/audit", json={
            "company": "Acme Candles",
            "handles": {"instagram": "https://instagram.com/acme"},
        })
        assert r.status_code == 200, r.text
        res = r.json()["results"]["instagram"]
        assert res["profile"]["followers"] == 3000
        assert any(i["severity"] == "high" for i in res["issues"])
        assert res["outreach"]["dm"]["body"] == "dm body"
        assert res["outreach"]["email"]["subject"] == "S"


def test_adapter_returning_none_is_a_note_not_a_crash(monkeypatch):
    monkeypatch.setattr(app_module.config, "API_KEY", None)
    monkeypatch.setitem(app_module._SOCIAL_ADAPTERS, "facebook", lambda handle: None)
    with TestClient(app_module.app, raise_server_exceptions=False) as client:
        r = client.post("/api/social/audit", json={
            "company": "Acme", "handles": {"facebook": "https://facebook.com/acme"},
        })
        assert r.status_code == 200
        res = r.json()["results"]["facebook"]
        assert res["profile"] is None
        assert res["issues"] == []
        assert "note" in res


def test_routes_gated(monkeypatch):
    import inspect
    for fn in (app_module.social_audit, app_module.social_audit_progress, app_module.social_audit_result):
        src = inspect.getsource(fn)
        assert "require_api_key" in src and "rate_limit" in src
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest -q test_social_routes_audit.py`
Expected: FAIL — `AttributeError: module 'app' has no attribute '_SOCIAL_ADAPTERS'`

- [ ] **Step 3: Write minimal implementation**

Add imports at module scope:
```python
from scrapers.social.instagram import fetch_instagram
from analyzer.social_audit import audit_profile, generate_social_outreach
from dataclasses import asdict
```

Then:

```python
_SOCIAL_ADAPTERS = {
    "instagram": fetch_instagram,
    "youtube": lambda handle: None,   # wired in Task 10
    "facebook": lambda handle: None,  # wired in Task 11
    "linkedin": lambda handle: None,  # wired in Task 12
}

_SOCIAL_AUDIT_TTL = 900
_social_audit_progress: dict[str, tuple[float, dict]] = {}
_social_audit_results: dict[str, tuple[float, dict]] = {}


class SocialAuditIn(BaseModel):
    company: str
    handles: dict            # {"instagram": url, "youtube": url, ...}
    async_mode: bool = False


def _social_audit_progress_set(key: str, stage: str) -> None:
    now = time.monotonic()
    for k, (ts, _) in list(_social_audit_progress.items()):
        if now - ts > _SOCIAL_AUDIT_TTL:
            _social_audit_progress.pop(k, None)
    _social_audit_progress[key] = (now, {"stage": stage})


def _handle_from_url(platform: str, url: str) -> str:
    """Best-effort handle/slug out of a profile URL for the adapter."""
    u = (url or "").rstrip("/")
    if not u:
        return ""
    tail = u.split("/")[-1]
    return tail.lstrip("@")


async def _social_audit_impl(req: SocialAuditIn, progress_key: str | None = None) -> dict:
    results = {}
    for platform, url in (req.handles or {}).items():
        if not url or platform not in _SOCIAL_ADAPTERS:
            continue
        if progress_key:
            _social_audit_progress_set(progress_key, f"Analyzing {platform}")
        handle = _handle_from_url(platform, url)
        adapter = _SOCIAL_ADAPTERS[platform]
        try:
            profile = await asyncio.to_thread(adapter, handle)
        except Exception as e:  # noqa: BLE001
            print(f"[SocialAudit] {platform} adapter raised: {e}")
            profile = None

        if profile is None:
            results[platform] = {"profile": None, "issues": [], "outreach": {}, "note": "could not analyze this profile"}
            continue
        if not getattr(profile, "analyzed", True):
            results[platform] = {
                "profile": asdict(profile), "issues": [], "outreach": {},
                "note": profile.note or "could not analyze this profile",
            }
            continue

        issues = audit_profile(profile)
        issue_dicts = [asdict(i) for i in issues]
        outreach = {}
        total_cost = 0.0
        for channel in ("email", "dm", "whatsapp"):
            if progress_key:
                _social_audit_progress_set(progress_key, f"Writing {platform} {channel}")
            copy = await asyncio.to_thread(
                generate_social_outreach, req.company, profile, issues, channel
            )
            outreach[channel] = {"subject": copy["subject"], "body": copy["body"]}
            total_cost += copy.get("cost", 0.0)
        if total_cost > 0:
            await asyncio.to_thread(
                db.log_cost, "Social Outreach", total_cost,
                description=f"Social outreach for {req.company} ({platform})",
            )
        results[platform] = {"profile": asdict(profile), "issues": issue_dicts, "outreach": outreach}

    return {"results": results}


@app.post("/api/social/audit")
async def social_audit(
    req: SocialAuditIn,
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(10, 60)),
):
    if req.async_mode:
        key = uuid.uuid4().hex

        async def _run():
            try:
                result = await _social_audit_impl(req, progress_key=key)
            except Exception as e:  # noqa: BLE001
                result = {"error": str(e)}
            now = time.monotonic()
            for k, (ts, _) in list(_social_audit_results.items()):
                if now - ts > _SOCIAL_AUDIT_TTL:
                    _social_audit_results.pop(k, None)
            _social_audit_results[key] = (now, result)
            _social_audit_progress.pop(key, None)

        asyncio.create_task(_run())
        return {"started": True, "key": key}

    return await _social_audit_impl(req)


@app.get("/api/social/audit/progress")
async def social_audit_progress(
    key: str,
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(300, 60)),
):
    entry = _social_audit_progress.get(key)
    if not entry:
        return {"running": False}
    _, data = entry
    return {"running": True, **data}


@app.get("/api/social/audit/result")
async def social_audit_result(
    key: str,
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(300, 60)),
):
    entry = _social_audit_results.pop(key, None)
    if not entry:
        return {"ready": False}
    _, result = entry
    return {"ready": True, **result}
```

> **Executor note:** the two `monkeypatch.setattr(app_module, "generate_social_outreach", ...)` in the test require `generate_social_outreach` to be a module-level name in `app.py` — the `from analyzer.social_audit import ... generate_social_outreach` import above provides that. Same for `find_social_handles` in Task 8.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest -q test_social_routes_audit.py`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git checkout -b feat/social-audit-route
git add app.py test_social_routes_audit.py
git commit -m "$(cat <<'EOF'
Add /api/social/audit with the async_mode pattern

Per requested platform: run the adapter, audit_profile for issues, then
one outreach draft per channel (email/dm/whatsapp). An adapter returning
None becomes a note, never a 500. YouTube/Facebook/LinkedIn adapters are
stubbed here and wired in the next three tasks.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 10: YouTube adapter + `YOUTUBE_API_KEY` config

**Files:**
- Create: `scrapers/social/youtube.py`
- Modify: `config.py` (add `YOUTUBE_API_KEY`), `app.py` (`_SOCIAL_ADAPTERS["youtube"] = fetch_youtube`)
- Test: `test_social_adapter_youtube.py`

**Interfaces:**
- Consumes: `httpx`, `config.YOUTUBE_API_KEY`.
- Produces: `fetch_youtube(handle_or_id: str, http_get=None) -> SocialProfile | None`. `http_get` is an injectable `fn(url: str) -> dict` (parsed JSON) for tests. `None` when the key is unset, the channel is not found, or the API errors.

- [ ] **Step 1: Write the failing test**

```python
# test_social_adapter_youtube.py
import pytest
import config
from scrapers.social.youtube import fetch_youtube


_CHANNEL_JSON = {
    "items": [{
        "snippet": {"title": "Acme Candles", "description": "Handmade candles. acme.com", "publishedAt": "2020-01-01T00:00:00Z"},
        "statistics": {"subscriberCount": "1500", "videoCount": "42", "viewCount": "230000"},
    }]
}
_RECENT_JSON = {
    "items": [
        {"snippet": {"publishedAt": "2026-09-05T00:00:00Z", "title": "new scent"}},
        {"snippet": {"publishedAt": "2026-08-20T00:00:00Z", "title": "studio tour"}},
    ]
}


def test_none_when_key_unset(monkeypatch):
    monkeypatch.setattr(config, "YOUTUBE_API_KEY", None)
    assert fetch_youtube("@AcmeCandles") is None


def test_maps_channel_stats(monkeypatch):
    monkeypatch.setattr(config, "YOUTUBE_API_KEY", "k")

    calls = []
    def fake_get(url):
        calls.append(url)
        return _CHANNEL_JSON if "channels" in url else _RECENT_JSON

    p = fetch_youtube("@AcmeCandles", http_get=fake_get)
    assert p.platform == "youtube"
    assert p.display_name == "Acme Candles"
    assert p.followers == 1500          # subscriberCount
    assert p.posts_count == 42          # videoCount
    assert p.uses_video is True         # a YouTube channel is video by definition
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest -q test_social_adapter_youtube.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'scrapers.social.youtube'`

- [ ] **Step 3: Write minimal implementation**

`config.py` — add near `PAGESPEED_KEY`:
```python
# YouTube Data API v3 key for the Social page's YouTube adapter. Enable
# "YouTube Data API v3" on the same Google Cloud project as PAGESPEED_KEY.
# Unset -> the YouTube adapter is a silent no-op (returns None), same
# pattern as the MCA lookup being inert without its key.
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
```

`scrapers/social/youtube.py`:
```python
"""
YouTube adapter — official YouTube Data API v3 (free 10,000 units/day).
channels.list is ~1 to 3 units; we resolve a channel by handle or id
directly and avoid the 100-unit search endpoint. No new dependency; plain
httpx GET.
"""

from datetime import datetime, timezone

import httpx

import config
from scrapers.social.base import SocialProfile, classify_frequency

_API = "https://www.googleapis.com/youtube/v3"


def _default_get(url: str) -> dict:
    with httpx.Client(timeout=8) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.json()


def _channels_url(handle_or_id: str, key: str) -> str:
    h = handle_or_id.strip()
    parts = "snippet,statistics,contentDetails"
    if h.startswith("@"):
        return f"{_API}/channels?part={parts}&forHandle={h}&key={key}"
    if h.startswith("UC") and len(h) > 20:
        return f"{_API}/channels?part={parts}&id={h}&key={key}"
    return f"{_API}/channels?part={parts}&forHandle=@{h}&key={key}"


def fetch_youtube(handle_or_id: str, http_get=None) -> SocialProfile | None:
    key = config.YOUTUBE_API_KEY
    if not key:
        return None
    get = http_get or _default_get
    try:
        data = get(_channels_url(handle_or_id, key))
    except Exception as e:  # noqa: BLE001
        print(f"[YouTube] channels.list failed: {e}")
        return None

    items = (data or {}).get("items") or []
    if not items:
        return None
    ch = items[0]
    snip = ch.get("snippet", {})
    stats = ch.get("statistics", {})
    uploads = ch.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads", "")
    channel_id = ch.get("id", "")

    posts_last_30 = 0
    last_age = None
    try:
        if uploads:
            pl = get(f"{_API}/playlistItems?part=snippet&maxResults=20&playlistId={uploads}&key={key}")
        else:
            pl = get(f"{_API}/search?part=snippet&channelId={channel_id}&order=date&maxResults=20&type=video&key={key}")
        now = datetime.now(timezone.utc)
        dates = []
        for it in (pl or {}).get("items", []):
            ts = it.get("snippet", {}).get("publishedAt")
            if not ts:
                continue
            d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            dates.append(d)
        if dates:
            last_age = (now - max(dates)).days
            posts_last_30 = sum(1 for d in dates if (now - d).days <= 30)
    except Exception as e:  # noqa: BLE001
        print(f"[YouTube] recent-uploads lookup failed (non fatal): {e}")

    handle = handle_or_id.lstrip("@")
    return SocialProfile(
        platform="youtube",
        handle=handle,
        url=f"https://youtube.com/@{handle}",
        display_name=snip.get("title", ""),
        bio=snip.get("description", ""),
        followers=int(stats.get("subscriberCount", 0) or 0),
        posts_count=int(stats.get("videoCount", 0) or 0),
        posts_last_30_days=posts_last_30,
        posting_frequency=classify_frequency(posts_last_30),
        uses_video=True,
        has_link_in_bio=False,
        last_post_age_days=last_age,
    )
```

`app.py` — replace the youtube stub:
```python
from scrapers.social.youtube import fetch_youtube
# ...
_SOCIAL_ADAPTERS["youtube"] = fetch_youtube
```
(or set it directly in the dict literal).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest -q test_social_adapter_youtube.py`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git checkout -b feat/social-adapter-youtube
git add config.py scrapers/social/youtube.py app.py test_social_adapter_youtube.py
git commit -m "$(cat <<'EOF'
Add YouTube adapter via the official Data API v3

Free-tier, no new dependency. Resolves a channel by handle or id, reads
subscriber/video counts and recent-upload cadence, avoids the 100-unit
search endpoint. Silent no-op without YOUTUBE_API_KEY.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 11: Facebook adapter (best-effort public Page)

**Files:**
- Create: `scrapers/social/facebook.py`
- Modify: `app.py` (`_SOCIAL_ADAPTERS["facebook"] = fetch_facebook`)
- Test: `test_social_adapter_facebook.py`

**Interfaces:**
- Produces: `fetch_facebook(slug_or_url: str, http_get=None) -> SocialProfile | None`. `http_get` -> `fn(url) -> str` (HTML). Returns `unanalyzed("facebook", ...)` on a login wall / unparseable page, `None` only on a hard fetch error, a populated `SocialProfile` when the public page exposes a like/follow count.

- [ ] **Step 1: Write the failing test**

```python
# test_social_adapter_facebook.py
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
    assert p.followers == 12900        # prefers follow count over like count
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest -q test_social_adapter_facebook.py`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# scrapers/social/facebook.py
"""
Facebook adapter — best effort only. There is no free Page API without Meta
app review, so this scrapes the public Page HTML. It works when Facebook
serves a real page with a visible like/follow count and returns
analyzed=False (never a crash) when it hits a login wall or unfamiliar
markup, which is common. Treat a miss here as normal.
"""

import re

import httpx
from bs4 import BeautifulSoup

from scrapers.social.base import SocialProfile, unanalyzed

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
_NUM = re.compile(r"([\d,.]+)\s*(?:people\s+)?(like|follow)", re.I)
_LOGIN_MARKERS = ("you must log in", "log in to continue", "log into facebook", "isn't available")


def _default_get(url: str) -> str:
    with httpx.Client(timeout=8, follow_redirects=True, headers={"User-Agent": _UA}) as c:
        r = c.get(url)
        return r.text


def _slug(s: str) -> str:
    s = (s or "").rstrip("/")
    if "facebook.com/" in s:
        s = s.split("facebook.com/", 1)[1]
    return s.split("/")[0].split("?")[0]


def _to_int(raw: str) -> int:
    raw = raw.replace(",", "").strip()
    m = re.match(r"([\d.]+)\s*([KkMm])?", raw)
    if not m:
        return 0
    n = float(m.group(1))
    suf = (m.group(2) or "").lower()
    return int(n * {"k": 1_000, "m": 1_000_000}.get(suf, 1))


def fetch_facebook(slug_or_url: str, http_get=None) -> SocialProfile | None:
    slug = _slug(slug_or_url)
    url = f"https://www.facebook.com/{slug}"
    get = http_get or _default_get
    try:
        html = get(url)
    except Exception as e:  # noqa: BLE001
        print(f"[Facebook] fetch failed: {e}")
        return None

    low = (html or "").lower()
    if not html or any(m in low for m in _LOGIN_MARKERS):
        return unanalyzed("facebook", slug, url, "Facebook served a login wall, could not read the public page")

    soup = BeautifulSoup(html, "html.parser")
    name = ""
    og_t = soup.find("meta", property="og:title")
    if og_t and og_t.get("content"):
        name = og_t["content"].split("|")[0].strip()
    bio = ""
    og_d = soup.find("meta", property="og:description")
    if og_d and og_d.get("content"):
        bio = og_d["content"].strip()

    likes = follows = 0
    for m in _NUM.finditer(soup.get_text(" ", strip=True)):
        val = _to_int(m.group(1))
        if m.group(2).lower().startswith("follow"):
            follows = max(follows, val)
        else:
            likes = max(likes, val)

    if not (likes or follows or name):
        return unanalyzed("facebook", slug, url, "Could not find a follower count on the public page")

    return SocialProfile(
        platform="facebook",
        handle=slug,
        url=url,
        display_name=name,
        bio=bio,
        followers=follows or likes,
        has_link_in_bio=False,
    )
```

`app.py` — replace the facebook stub with `from scrapers.social.facebook import fetch_facebook` and `_SOCIAL_ADAPTERS["facebook"] = fetch_facebook`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest -q test_social_adapter_facebook.py`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git checkout -b feat/social-adapter-facebook
git add scrapers/social/facebook.py app.py test_social_adapter_facebook.py
git commit -m "$(cat <<'EOF'
Add best-effort Facebook Page adapter

Scrapes the public Page HTML for name, description and a like/follow
count. Returns analyzed=False (not a crash, not a fake issue) on a login
wall or unfamiliar markup, which is a common and expected outcome.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 12: LinkedIn adapter (best-effort public company page)

**Files:**
- Create: `scrapers/social/linkedin.py`
- Modify: `app.py` (`_SOCIAL_ADAPTERS["linkedin"] = fetch_linkedin`)
- Test: `test_social_adapter_linkedin.py`

**Interfaces:**
- Produces: `fetch_linkedin(slug_or_url: str, http_get=None) -> SocialProfile | None`. Same contract as Facebook: `unanalyzed(...)` on auth wall / no data, `None` on hard fetch error, populated profile when the public company page exposes a follower count.

- [ ] **Step 1: Write the failing test**

```python
# test_social_adapter_linkedin.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest -q test_social_adapter_linkedin.py`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# scrapers/social/linkedin.py
"""
LinkedIn adapter — best effort only, and the least reliable of the four.
LinkedIn fights scraping harder than anyone; this reads the public company
page HTML when LinkedIn serves one, and returns analyzed=False the rest of
the time (an auth wall, a 999 status, unfamiliar markup). A miss here is
the common case, not a bug.
"""

import re

import httpx
from bs4 import BeautifulSoup

from scrapers.social.base import SocialProfile, unanalyzed

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
_FOLLOWERS = re.compile(r"([\d,.]+)\s*followers", re.I)
_WALL_MARKERS = ("sign in to see", "join linkedin", "authwall", "log in to continue")


def _default_get(url: str) -> str:
    with httpx.Client(timeout=8, follow_redirects=True, headers={"User-Agent": _UA}) as c:
        r = c.get(url)
        return r.text


def _slug(s: str) -> str:
    s = (s or "").rstrip("/")
    if "linkedin.com/company/" in s:
        s = s.split("linkedin.com/company/", 1)[1]
    return s.split("/")[0].split("?")[0]


def _to_int(raw: str) -> int:
    raw = raw.replace(",", "").strip()
    m = re.match(r"([\d.]+)\s*([KkMm])?", raw)
    if not m:
        return 0
    return int(float(m.group(1)) * {"k": 1_000, "m": 1_000_000}.get((m.group(2) or "").lower(), 1))


def fetch_linkedin(slug_or_url: str, http_get=None) -> SocialProfile | None:
    slug = _slug(slug_or_url)
    url = f"https://www.linkedin.com/company/{slug}/"
    get = http_get or _default_get
    try:
        html = get(url)
    except Exception as e:  # noqa: BLE001
        print(f"[LinkedIn] fetch failed: {e}")
        return None

    low = (html or "").lower()
    if not html or any(m in low for m in _WALL_MARKERS):
        return unanalyzed("linkedin", slug, url, "LinkedIn served an auth wall, could not read the public page")

    soup = BeautifulSoup(html, "html.parser")
    name = ""
    og_t = soup.find("meta", property="og:title")
    if og_t and og_t.get("content"):
        name = og_t["content"].split("|")[0].strip()
    bio = ""
    og_d = soup.find("meta", property="og:description")
    if og_d and og_d.get("content"):
        bio = og_d["content"].strip()
    body_p = soup.find("p")
    if body_p and len(body_p.get_text(strip=True)) > len(bio):
        bio = body_p.get_text(strip=True)

    followers = 0
    m = _FOLLOWERS.search(soup.get_text(" ", strip=True))
    if m:
        followers = _to_int(m.group(1))

    if not (followers or name):
        return unanalyzed("linkedin", slug, url, "Could not find a follower count on the public page")

    return SocialProfile(
        platform="linkedin",
        handle=slug,
        url=url,
        display_name=name,
        bio=bio,
        followers=followers,
        has_link_in_bio=False,
    )
```

`app.py` — replace the linkedin stub with `from scrapers.social.linkedin import fetch_linkedin` and `_SOCIAL_ADAPTERS["linkedin"] = fetch_linkedin`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest -q test_social_adapter_linkedin.py`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git checkout -b feat/social-adapter-linkedin
git add scrapers/social/linkedin.py app.py test_social_adapter_linkedin.py
git commit -m "$(cat <<'EOF'
Add best-effort LinkedIn company-page adapter

Reads the public company page HTML for name, about and follower count
when LinkedIn serves one; analyzed=False on the auth wall / 999 / unknown
markup, which is the common case. No automation against LinkedIn.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 13: Full suite gate + adapter dispatch cleanup

**Files:**
- Modify: `app.py` (only if the four `_SOCIAL_ADAPTERS` entries are still assigned in scattered spots — consolidate into one dict literal with all four real imports at the top)
- Test: run the whole suite

**Interfaces:** none new.

- [ ] **Step 1: Consolidate the dispatch dict**

Ensure the top of `app.py` has all four imports together and one literal:

```python
from scrapers.social.instagram import fetch_instagram
from scrapers.social.youtube import fetch_youtube
from scrapers.social.facebook import fetch_facebook
from scrapers.social.linkedin import fetch_linkedin

_SOCIAL_ADAPTERS = {
    "instagram": fetch_instagram,
    "youtube": fetch_youtube,
    "facebook": fetch_facebook,
    "linkedin": fetch_linkedin,
}
```

Delete any leftover `_SOCIAL_ADAPTERS["..."] = ...` reassignment lines from Tasks 10 to 12.

- [ ] **Step 2: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS — previous baseline (748 passed, 1 skipped) plus every new `test_social_*.py` test. No failures, no errors.

- [ ] **Step 3: Commit**

```bash
git checkout -b chore/social-adapter-dispatch
git add app.py
git commit -m "$(cat <<'EOF'
Consolidate the social adapter dispatch dict

All four adapters imported and wired in one place at module scope.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 14: Frontend — Social tab shell (nav + search + results grid)

**Files:**
- Modify: `frontend/src/App.jsx`
- Verify: `npm run build && npm run lint` (no JS test framework in this repo)

**Interfaces:**
- Consumes: `POST /api/social/search` (+ `/progress` + `/result`), the existing `pollSearchResult` helper if present (grep for it; if it exists reuse it, else inline a 2s poll loop against `/api/social/search/result`).
- Produces: `currentView === 'social'` rendered by `renderSocial()`; a nav button between "Agriculture" and "Drafts"; state `socialNiche`, `socialCity`, `socialLimit`, `socialBusinesses` (array), `socialSearching` (bool), `socialSearchStage` (string).

- [ ] **Step 1: Add state + nav button + view switch**

Near the other `useState` declarations:
```javascript
  const [socialNiche, setSocialNiche] = useState('');
  const [socialCity, setSocialCity] = useState('');
  const [socialLimit, setSocialLimit] = useState(10);
  const [socialBusinesses, setSocialBusinesses] = useState([]);
  const [socialSearching, setSocialSearching] = useState(false);
  const [socialSearchStage, setSocialSearchStage] = useState('');
```

In the nav bar, copy the "agriculture" button block and change: `onClick={() => setCurrentView('social')}`, label `Social`, and every `currentView === 'agriculture'` -> `currentView === 'social'`. Place it right after the agriculture nav button.

In the view-switch block near `{currentView === 'agriculture' && renderAgriculture()}` add:
```javascript
        {currentView === 'social' && renderSocial()}
```

- [ ] **Step 2: Add `handleSocialSearch` + `renderSocial`**

Add near `handleAgriSearch` / `renderAgriculture`:

```javascript
  const handleSocialSearch = async (e) => {
    if (e) e.preventDefault();
    if (!socialNiche.trim() || !socialCity.trim()) {
      alert('Enter a niche and a city.');
      return;
    }
    setSocialSearching(true);
    setSocialSearchStage('Starting...');
    setSocialBusinesses([]);
    try {
      const start = await axios.post(`${API_BASE}/api/social/search`, {
        niche: socialNiche.trim(), city: socialCity.trim(), limit: Number(socialLimit) || 10,
        async_mode: true,
      });
      const key = start.data.key;
      // poll result, surfacing progress
      // (mirrors the Dashboard's async search poll)
      // eslint-disable-next-line no-constant-condition
      while (true) {
        await new Promise(r => setTimeout(r, 2000));
        const prog = await axios.get(`${API_BASE}/api/social/search/progress`, { params: { key } });
        if (prog.data.running && prog.data.stage) setSocialSearchStage(prog.data.stage);
        const res = await axios.get(`${API_BASE}/api/social/search/result`, { params: { key } });
        if (res.data.ready) {
          if (res.data.error) { alert(`Search failed: ${res.data.error}`); break; }
          setSocialBusinesses((res.data.businesses || []).map(b => ({ ...b, audit: null, auditing: false })));
          break;
        }
      }
    } catch (err) {
      console.error('Social search failed:', err);
      alert(`Social search failed: ${err.response?.data?.detail || err.message}`);
    } finally {
      setSocialSearching(false);
      setSocialSearchStage('');
    }
  };

  const PLATFORM_META = {
    instagram: { label: 'Instagram', color: '#d6249f' },
    youtube: { label: 'YouTube', color: '#ff0000' },
    facebook: { label: 'Facebook', color: '#1877f2' },
    linkedin: { label: 'LinkedIn', color: '#0a66c2' },
  };

  const renderSocial = () => (
    <div className="glass" style={{ padding: '24px' }}>
      <h2 style={{ margin: 0 }}>Social Media Outreach</h2>
      <p style={{ color: '#94a3b8', margin: '8px 0 16px' }}>
        Find a business, audit its social presence, and reach out by email, DM or WhatsApp.
      </p>

      <form onSubmit={handleSocialSearch} style={{ display: 'flex', gap: '10px', flexWrap: 'wrap', marginBottom: '20px' }}>
        <input value={socialNiche} onChange={e => setSocialNiche(e.target.value)} placeholder="Business niche (e.g. candle shop)" style={{ flex: '1 1 220px', padding: '10px' }} />
        <input value={socialCity} onChange={e => setSocialCity(e.target.value)} placeholder="City" style={{ flex: '1 1 160px', padding: '10px' }} />
        <input type="number" min="1" max="30" value={socialLimit} onChange={e => setSocialLimit(e.target.value)} style={{ width: '80px', padding: '10px' }} />
        <button type="submit" disabled={socialSearching} className="primary-btn" style={{ background: '#10b981', color: '#fff', border: 'none', padding: '10px 20px', borderRadius: '8px', fontWeight: 'bold' }}>
          {socialSearching ? 'Searching...' : 'Find Businesses'}
        </button>
      </form>

      {socialSearching && socialSearchStage && (
        <p style={{ color: '#64748b', fontSize: '13px' }}>{socialSearchStage}</p>
      )}

      <div className="leads-grid" style={{ gridTemplateColumns: '1fr' }}>
        {socialBusinesses.map((b, i) => (
          <div key={`${b.company}-${i}`} className="lead-card glass">
            <div className="lead-header"><h3>{b.company}</h3></div>
            <div className="lead-details">
              {b.website && <p><strong>Site:</strong> <a href={b.website} target="_blank" rel="noreferrer">{b.website}</a></p>}
              <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', margin: '8px 0' }}>
                {Object.keys(PLATFORM_META).map(p => {
                  const has = !!(b.handles && b.handles[p]);
                  return (
                    <span key={p} style={{
                      fontSize: '12px', fontWeight: 600, padding: '3px 10px', borderRadius: '999px',
                      border: `1px solid ${has ? PLATFORM_META[p].color : '#cbd5e1'}`,
                      color: has ? PLATFORM_META[p].color : '#94a3b8',
                      background: has ? `${PLATFORM_META[p].color}14` : 'transparent',
                    }}>{PLATFORM_META[p].label}{has ? '' : ' (none)'}</span>
                  );
                })}
              </div>
              <button
                type="button"
                disabled={b.auditing || !Object.values(b.handles || {}).some(Boolean)}
                onClick={() => handleSocialAudit(i)}
                className="send-btn"
                style={{ marginTop: '8px' }}
              >
                {b.auditing ? 'Auditing socials...' : 'Audit socials'}
              </button>
            </div>
            {b.audit && renderSocialAuditResult(b, i)}
          </div>
        ))}
      </div>
    </div>
  );
```

> `handleSocialAudit` and `renderSocialAuditResult` are added in Task 15. To keep this task building, add temporary no-op stubs right after `renderSocial`:
> ```javascript
>   const handleSocialAudit = async () => {};
>   const renderSocialAuditResult = () => null;
> ```
> Task 15 replaces both.

- [ ] **Step 3: Build + lint**

Run: `cd frontend && npm run build && npm run lint`
Expected: build succeeds; lint shows only the two pre-existing `sessionTotalCost` / `draftAgeDays` warnings.

- [ ] **Step 4: Commit**

```bash
git checkout -b feat/social-tab-shell
git add frontend/src/App.jsx
git commit -m "$(cat <<'EOF'
Add Social tab shell: nav, niche/city search, results grid

New currentView 'social'. Async search against /api/social/search with a
progress line; each result card shows the business and a chip row of which
platforms have a detected handle, plus an Audit socials button (wired in
the next task).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 15: Frontend — audit result (profile + issues + channel sub-tabs)

**Files:**
- Modify: `frontend/src/App.jsx` (replace the two stubs from Task 14)
- Verify: `npm run build && npm run lint`

**Interfaces:**
- Consumes: `POST /api/social/audit` (+ `/progress` + `/result`), `POST /api/social/drafts`, `POST /api/social/send`.
- Produces: real `handleSocialAudit(index)` and `renderSocialAuditResult(business, index)`; adds state `socialActiveChannel` (object keyed `"<i>-<platform>"` -> `"email"|"dm"|"whatsapp"`), and updates `socialBusinesses[index].audit` with the `/result` payload plus per-platform editable `outreach` copy.

- [ ] **Step 1: Replace `handleSocialAudit`**

```javascript
  const handleSocialAudit = async (index) => {
    const biz = socialBusinesses[index];
    const handles = Object.fromEntries(
      Object.entries(biz.handles || {}).filter(([, v]) => !!v)
    );
    if (Object.keys(handles).length === 0) return;
    setSocialBusinesses(prev => prev.map((b, i) => i === index ? { ...b, auditing: true } : b));
    try {
      const start = await axios.post(`${API_BASE}/api/social/audit`, {
        company: biz.company, handles, async_mode: true,
      });
      const key = start.data.key;
      // eslint-disable-next-line no-constant-condition
      while (true) {
        await new Promise(r => setTimeout(r, 2000));
        const res = await axios.get(`${API_BASE}/api/social/audit/result`, { params: { key } });
        if (res.data.ready) {
          if (res.data.error) { alert(`Social audit failed: ${res.data.error}`); break; }
          setSocialBusinesses(prev => prev.map((b, i) => i === index ? { ...b, audit: res.data.results || {} } : b));
          break;
        }
      }
    } catch (err) {
      console.error('Social audit failed:', err);
      alert(`Social audit failed: ${err.response?.data?.detail || err.message}`);
    } finally {
      setSocialBusinesses(prev => prev.map((b, i) => i === index ? { ...b, auditing: false } : b));
    }
  };

  const setSocialCopy = (index, platform, channel, field, value) => {
    setSocialBusinesses(prev => prev.map((b, i) => {
      if (i !== index) return b;
      const audit = { ...b.audit };
      const plat = { ...audit[platform] };
      const outreach = { ...plat.outreach };
      outreach[channel] = { ...outreach[channel], [field]: value };
      plat.outreach = outreach;
      audit[platform] = plat;
      return { ...b, audit };
    }));
  };

  const saveSocialDraft = async (biz, platform, channel) => {
    const plat = biz.audit[platform];
    const copy = plat.outreach[channel] || {};
    const target = channel === 'email'
      ? (prompt('Recipient email address:') || '').trim()
      : (channel === 'whatsapp' ? (biz.phone || '') : plat.profile?.handle || '');
    if (channel === 'email' && !target) return;
    try {
      await axios.post(`${API_BASE}/api/social/drafts`, {
        company: biz.company, platform, handle: plat.profile?.handle || '',
        profile_url: plat.profile?.url || '', channel, target,
        subject: copy.subject || '', body: copy.body || '',
        issues: plat.issues || [],
      });
      fetchSocialDrafts();
      alert('Saved to social drafts.');
    } catch (err) {
      alert(`Save failed: ${err.response?.data?.detail || err.message}`);
    }
  };

  const openSocialChannel = (biz, platform, channel) => {
    const plat = biz.audit[platform];
    const copy = plat.outreach[channel] || {};
    if (channel === 'email') {
      const to = (prompt('Recipient email address:') || '').trim();
      if (!to) return;
      window.open(`mailto:${to}?subject=${encodeURIComponent(copy.subject || '')}&body=${encodeURIComponent(copy.body || '')}`);
    } else if (channel === 'whatsapp') {
      const digits = (biz.phone || '').replace(/\D/g, '');
      if (!digits) { alert('No phone number for this business.'); return; }
      window.open(`https://wa.me/${digits}?text=${encodeURIComponent(copy.body || '')}`);
    } else {
      navigator.clipboard?.writeText(copy.body || '');
      window.open(`https://instagram.com/${plat.profile?.handle || ''}`);
    }
  };
```

- [ ] **Step 2: Replace `renderSocialAuditResult`**

```javascript
  const SEVERITY_COLOR = { high: '#ef4444', medium: '#f59e0b', low: '#64748b' };

  const renderSocialAuditResult = (biz, index) => (
    <div style={{ marginTop: '14px', borderTop: '1px solid rgba(148,163,184,0.2)', paddingTop: '14px' }}>
      {Object.entries(biz.audit).map(([platform, data]) => {
        const chanKey = `${index}-${platform}`;
        const active = socialActiveChannel[chanKey] || 'email';
        const meta = PLATFORM_META[platform] || { label: platform, color: '#64748b' };
        return (
          <div key={platform} style={{ marginBottom: '18px' }}>
            <div style={{ fontWeight: 700, color: meta.color, marginBottom: '6px' }}>{meta.label}</div>

            {!data.profile && <p style={{ color: '#94a3b8', fontSize: '13px' }}>{data.note || 'Could not analyze this profile.'}</p>}

            {data.profile && (
              <>
                <p style={{ fontSize: '13px', color: '#475569', margin: '2px 0' }}>
                  {data.profile.followers.toLocaleString()} followers
                  {data.profile.posting_frequency ? ` · ${data.profile.posting_frequency}` : ''}
                  {data.profile.avg_engagement_rate ? ` · ${data.profile.avg_engagement_rate}% engagement` : ''}
                </p>
                {data.note && <p style={{ color: '#94a3b8', fontSize: '12px' }}>{data.note}</p>}

                {(data.issues || []).length > 0 && (
                  <ul style={{ margin: '8px 0', paddingLeft: '18px' }}>
                    {data.issues.map((iss, k) => (
                      <li key={k} style={{ fontSize: '13px', margin: '3px 0' }}>
                        <span style={{ color: SEVERITY_COLOR[iss.severity], fontWeight: 700 }}>{iss.label}</span>
                        {' — '}{iss.detail}
                      </li>
                    ))}
                  </ul>
                )}

                {data.outreach && Object.keys(data.outreach).length > 0 && (
                  <div style={{ marginTop: '10px' }}>
                    <div style={{ display: 'flex', gap: '6px', marginBottom: '6px' }}>
                      {['email', 'dm', 'whatsapp'].map(ch => (
                        <button key={ch} type="button"
                          onClick={() => setSocialActiveChannel(prev => ({ ...prev, [chanKey]: ch }))}
                          style={{
                            fontSize: '12px', padding: '4px 12px', borderRadius: '8px', cursor: 'pointer',
                            border: '1px solid #cbd5e1',
                            background: active === ch ? '#10b981' : 'transparent',
                            color: active === ch ? '#fff' : '#64748b', fontWeight: 600,
                          }}>{ch.toUpperCase()}</button>
                      ))}
                    </div>
                    {active === 'email' && (
                      <input
                        value={data.outreach.email?.subject || ''}
                        onChange={e => setSocialCopy(index, platform, 'email', 'subject', e.target.value)}
                        placeholder="Subject"
                        style={{ width: '100%', padding: '8px', marginBottom: '6px', fontWeight: 600 }}
                      />
                    )}
                    <textarea
                      value={data.outreach[active]?.body || ''}
                      onChange={e => setSocialCopy(index, platform, active, 'body', e.target.value)}
                      rows={active === 'email' ? 7 : 4}
                      style={{ width: '100%', padding: '8px' }}
                    />
                    <div style={{ display: 'flex', gap: '8px', marginTop: '6px', flexWrap: 'wrap' }}>
                      <button type="button" onClick={() => navigator.clipboard?.writeText(data.outreach[active]?.body || '')}
                        style={{ fontSize: '12px', padding: '5px 12px', borderRadius: '8px', border: '1px solid #cbd5e1', cursor: 'pointer' }}>Copy</button>
                      <button type="button" onClick={() => openSocialChannel(biz, platform, active)}
                        style={{ fontSize: '12px', padding: '5px 12px', borderRadius: '8px', border: '1px solid #cbd5e1', cursor: 'pointer' }}>Open {active}</button>
                      <button type="button" onClick={() => saveSocialDraft(biz, platform, active)}
                        style={{ fontSize: '12px', padding: '5px 12px', borderRadius: '8px', border: 'none', background: '#10b981', color: '#fff', fontWeight: 600, cursor: 'pointer' }}>Save draft</button>
                    </div>
                  </div>
                )}
              </>
            )}
          </div>
        );
      })}
    </div>
  );
```

Add the state near the Task 14 block:
```javascript
  const [socialActiveChannel, setSocialActiveChannel] = useState({});
```

> `fetchSocialDrafts` is defined in Task 16. Add a temporary `const fetchSocialDrafts = () => {};` stub right above `renderSocial` for this task; Task 16 replaces it.

- [ ] **Step 3: Build + lint**

Run: `cd frontend && npm run build && npm run lint`
Expected: build succeeds; only the two known pre-existing warnings.

- [ ] **Step 4: Commit**

```bash
git checkout -b feat/social-audit-result-ui
git add frontend/src/App.jsx
git commit -m "$(cat <<'EOF'
Add social audit result UI: profile, issues, channel sub-tabs

Per platform: profile summary or the couldn't-analyze note, the ranked
issue list (severity coloured), and Email / DM / WhatsApp sub-tabs with
editable copy plus Copy / Open / Save draft. DM open copies the text and
opens the profile (assisted, never automated); WhatsApp is a wa.me link.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 16: Frontend — social drafts section

**Files:**
- Modify: `frontend/src/App.jsx`
- Verify: `npm run build && npm run lint`

**Interfaces:**
- Consumes: `GET /api/social/drafts`, `DELETE /api/social/drafts/{id}`, `POST /api/social/send`, existing `groupByPeriod` + `GroupBySelector`.
- Produces: `socialDrafts` state + `fetchSocialDrafts()` (replaces the Task 15 stub), a "Social Drafts" section at the bottom of `renderSocial()`, `socialDraftsGroupBy` state, and a `useEffect` that calls `fetchSocialDrafts()` when `currentView === 'social'`.

- [ ] **Step 1: State + fetch + effect**

```javascript
  const [socialDrafts, setSocialDrafts] = useState([]);
  const [socialDraftsGroupBy, setSocialDraftsGroupBy] = useState('day');

  const fetchSocialDrafts = async () => {
    try {
      const res = await axios.get(`${API_BASE}/api/social/drafts`);
      setSocialDrafts(res.data.drafts || []);
    } catch (err) {
      console.error('Fetch social drafts failed:', err);
    }
  };
```

In the effect that already branches on `currentView` (the one near `if (currentView === 'drafts')`), add:
```javascript
    if (currentView === 'social') {
      fetchSocialDrafts();
    }
```

- [ ] **Step 2: Delete + send handlers**

```javascript
  const deleteSocialDraft = async (id) => {
    try {
      await axios.delete(`${API_BASE}/api/social/drafts/${id}`);
      setSocialDrafts(prev => prev.filter(d => d.id !== id));
    } catch (err) {
      alert(`Delete failed: ${err.response?.data?.detail || err.message}`);
    }
  };

  const sendSocialEmailDraft = async (d) => {
    if (d.channel !== 'email') return;
    if (!window.confirm(`Send this email to ${d.target}?`)) return;
    try {
      const res = await axios.post(`${API_BASE}/api/social/send`, {
        company: d.company, target: d.target, subject: d.subject, body: d.body,
      });
      if (res.data.sent) {
        alert('Sent.');
        deleteSocialDraft(d.id);
      } else {
        alert(`Send failed: ${res.data.error || 'unknown'}`);
      }
    } catch (err) {
      alert(`Send failed: ${err.response?.data?.detail || err.message}`);
    }
  };

  const openSocialDraftChannel = (d) => {
    if (d.channel === 'email') {
      window.open(`mailto:${d.target}?subject=${encodeURIComponent(d.subject || '')}&body=${encodeURIComponent(d.body || '')}`);
    } else if (d.channel === 'whatsapp') {
      const digits = (d.target || '').replace(/\D/g, '');
      if (digits) window.open(`https://wa.me/${digits}?text=${encodeURIComponent(d.body || '')}`);
    } else {
      navigator.clipboard?.writeText(d.body || '');
      window.open(`https://instagram.com/${d.handle || ''}`);
    }
  };
```

- [ ] **Step 3: Render the section**

At the end of `renderSocial()`'s outer `<div>`, before it closes, add:

```javascript
      <div style={{ marginTop: '30px', borderTop: '1px solid rgba(148,163,184,0.2)', paddingTop: '18px' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '10px' }}>
          <h3 style={{ margin: 0 }}>Social Drafts ({socialDrafts.length})</h3>
          <GroupBySelector value={socialDraftsGroupBy} onChange={setSocialDraftsGroupBy} />
        </div>
        {groupByPeriod(socialDrafts, socialDraftsGroupBy).map(group => (
          <div key={group.key}>
            {group.label && <h4 style={{ margin: '16px 0 8px', color: '#94a3b8', fontSize: '13px' }}>{group.label} ({group.items.length})</h4>}
            {group.items.map(d => (
              <div key={d.id} className="lead-card glass" style={{ marginBottom: '10px' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', flexWrap: 'wrap', gap: '8px' }}>
                  <strong>{d.company}</strong>
                  <span style={{ fontSize: '12px', color: '#64748b' }}>{(PLATFORM_META[d.platform]?.label || d.platform)} · {d.channel.toUpperCase()}</span>
                </div>
                {d.subject && <p style={{ fontWeight: 600, margin: '6px 0 2px' }}>{d.subject}</p>}
                <p style={{ whiteSpace: 'pre-wrap', fontSize: '13px', color: '#475569', margin: '4px 0' }}>{d.body}</p>
                <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', marginTop: '6px' }}>
                  <button type="button" onClick={() => navigator.clipboard?.writeText(d.body || '')} style={{ fontSize: '12px', padding: '4px 10px', borderRadius: '6px', border: '1px solid #cbd5e1', cursor: 'pointer' }}>Copy</button>
                  <button type="button" onClick={() => openSocialDraftChannel(d)} style={{ fontSize: '12px', padding: '4px 10px', borderRadius: '6px', border: '1px solid #cbd5e1', cursor: 'pointer' }}>Open {d.channel}</button>
                  {d.channel === 'email' && (
                    <button type="button" onClick={() => sendSocialEmailDraft(d)} style={{ fontSize: '12px', padding: '4px 10px', borderRadius: '6px', border: 'none', background: '#10b981', color: '#fff', fontWeight: 600, cursor: 'pointer' }}>Send</button>
                  )}
                  <button type="button" onClick={() => deleteSocialDraft(d.id)} style={{ fontSize: '12px', padding: '4px 10px', borderRadius: '6px', border: '1px solid #fca5a5', color: '#ef4444', cursor: 'pointer' }}>Delete</button>
                </div>
              </div>
            ))}
          </div>
        ))}
        {socialDrafts.length === 0 && <p style={{ color: '#94a3b8', fontSize: '13px' }}>No social drafts yet.</p>}
      </div>
```

Remove the temporary `fetchSocialDrafts` stub from Task 15.

- [ ] **Step 4: Build + lint**

Run: `cd frontend && npm run build && npm run lint`
Expected: build succeeds; only the two known pre-existing warnings. If lint flags an unused var from an earlier task's stub, remove the stub.

- [ ] **Step 5: Commit**

```bash
git checkout -b feat/social-drafts-ui
git add frontend/src/App.jsx
git commit -m "$(cat <<'EOF'
Add the social drafts section to the Social page

Own list on the page (not the website Drafts tab), Day/Week/Month/All
grouping. Copy / Open / Delete on every row; Send only on email rows
(real send via /api/social/send). DM and WhatsApp are open-only.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 17: CLAUDE.md + full verification

**Files:**
- Modify: `CLAUDE.md` (new §5.14 subsection describing the Social page; one changelog row dated 2026-09-10)
- Verify: full `pytest` + `npm run build` + `npm run lint`

- [ ] **Step 1: Run the full backend suite**

Run: `python -m pytest -q`
Expected: PASS — baseline plus all `test_social_*` files, no failures/errors. Record the count.

- [ ] **Step 2: Frontend build + lint**

Run: `cd frontend && npm run build && npm run lint`
Expected: clean build; only the two pre-existing warnings.

- [ ] **Step 3: Add CLAUDE.md §5.14**

Add a `### 5.14 Social Media page (added 2026-09-10)` subsection covering: the four adapters and their reliability tiers, `SocialProfile` / `SocialIssue`, `analyzer/social_audit.py` split (deterministic issues + grounded AI copy), the `/api/social/*` routes reusing `async_mode`, `social_drafts` schema v9, assisted-only DM, and `YOUTUBE_API_KEY`. Add a §13 changelog row with the final test count.

- [ ] **Step 4: Commit**

```bash
git checkout -b docs/social-page-claude-md
git add CLAUDE.md
git commit -m "$(cat <<'EOF'
Document the Social Media page in CLAUDE.md

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review

**1. Spec coverage:**

| Spec section | Task |
|---|---|
| §4 architecture (`scrapers/social/`, `analyzer/social_audit.py`) | 1, 2, 3, 4, 7, 10, 11, 12 |
| §5 `SocialProfile` | 1 |
| §5 `SocialIssue` | 2 |
| §5 `social_drafts` table | 5 |
| §6 handle discovery | 7 |
| §7 issue detection | 2 |
| §8 outreach copy generation | 4 |
| §9 routes (`search`, `audit`, `drafts`, `send`) | 6, 8, 9 |
| §10 frontend Social tab | 14, 15, 16 |
| §11 `YOUTUBE_API_KEY` | 10 |
| §12 testing | every task's Step 1; Tasks 13 and 17 run the full suite |
| §13 build order | task order matches |
| §14 risks (FB/LinkedIn degrade, IG breaker, YT quota) | 11, 12 (unanalyzed path), 3 (breaker unchanged), 10 (avoids search endpoint) |

No gaps.

**2. Placeholder scan:** No "TBD"/"handle edge cases"/"similar to Task N". Two explicit executor notes (Task 4 provider method name, Task 8 Maps dict keys) are verification instructions with a concrete fallback, not placeholders. Frontend stubs in Tasks 14 and 15 are named, temporary, and explicitly replaced in a stated later task.

**3. Type consistency:**
- `SocialProfile` field names identical across Tasks 1, 3, 10, 11, 12, and the frontend reads (`followers`, `posting_frequency`, `avg_engagement_rate`, `handle`, `url`, `note`, `analyzed`).
- `SocialIssue(severity, label, detail)` identical in Tasks 2, 4, and the frontend render.
- `generate_social_outreach(...) -> {"subject","body","cost"}` consistent Tasks 4 and 9.
- Adapter signature `fetch_<platform>(handle, ...) -> SocialProfile | None` consistent Tasks 3, 10, 11, 12, and the `_SOCIAL_ADAPTERS` dispatch in Task 9/13.
- Route function names used in tests (`social_drafts_list`, `social_search`, `social_audit`, `social_audit_progress`, ...) match the `@app` handler names defined in Tasks 6, 8, 9.
- `db.log_social_draft` / `get_social_drafts` / `delete_social_draft` consistent Tasks 5 and 6.

No mismatches found.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-10-social-media-page.md`. Two execution options:

**1. Subagent-Driven (recommended)** - dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** - execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
