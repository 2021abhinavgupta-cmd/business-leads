# Social Media Page — Design

**Date:** 2026-09-10
**Status:** Draft, awaiting review
**Requested as:** "for social media i want a completely different page for it ...
where i can mail or dm or whatsapp social media handles what is the issues"

---

## 1. Goal

A second top-level page, parallel to the website-audit Dashboard, that audits a
business's **social media presence** instead of its website, surfaces concrete
issues with that presence, and produces outreach copy for email / Instagram DM /
WhatsApp — each citing the real issues found.

The website Dashboard is unchanged. This is an additive, separate flow with its
own search, its own audit pipeline, its own drafts.

## 2. Non-goals

- No change to the website `/api/audit` pipeline, its drafts, or its Dashboard.
- No automated Instagram DM sending. DM outreach is **assisted only**: generate
  the message, one click copies it and opens `instagram.com/direct/t/...` (or the
  profile) in the browser. Same safe pattern as the existing `wa.me` links. The
  existing `InstagramScraper.send_dm()` stays unused by this feature.
- No paid social APIs. YouTube Data API v3 is free-tier; everything else is the
  existing `instagrapi` path or best-effort public HTML scrape.
- No scheduled/automated social sending. Everything is operator-triggered from
  the page, like the website Drafts inbox.

## 3. Platform feasibility (the load-bearing constraint)

The four platforms are not equal. The design treats each as a pluggable adapter
so the unreliable ones degrade to "couldn't analyze" without breaking the page.

| Platform  | Source                          | Reliability | Notes |
|-----------|---------------------------------|-------------|-------|
| Instagram | `instagrapi` (existing scraper) | Medium      | Burner account, challenge circuit breaker already in place. Real analytics. |
| YouTube   | YouTube Data API v3 (official)  | High        | Free 10,000 units/day. Real subscriber/view/video-cadence data. No ToS risk. New `YOUTUBE_API_KEY`. |
| Facebook  | Public Page HTML scrape         | Low         | No free API without Meta app review. Can sometimes read Page name, like/follow count, category. Breaks whenever FB changes markup or shows a login wall. |
| LinkedIn  | Public company-page HTML scrape | Lowest      | Most aggressive anti-scraper on the web. Often returns nothing. ToS/ban risk. Followers + about + latest post at best. |

**FB and LinkedIn will frequently return `None` / "profile could not be
analyzed".** That is inherent, surfaced honestly in the UI, and never treated as
an error. Same "absent signal is not a finding" discipline as the rest of the
codebase (CLAUDE.md §8).

## 4. Architecture

```
scrapers/social/
├── __init__.py
├── base.py           # SocialProfile dataclass, SocialAdapter protocol
├── instagram.py      # wraps the existing InstagramScraper -> SocialProfile
├── youtube.py        # YouTube Data API v3 client -> SocialProfile
├── facebook.py       # best-effort public Page scrape -> SocialProfile | None
└── linkedin.py       # best-effort public company-page scrape -> SocialProfile | None

analyzer/
└── social_audit.py   # SocialProfile -> ranked list of SocialIssue + outreach copy

storage/db.py         # + social_drafts table (schema bump), CRUD helpers

app.py                # + /api/social/* routes (search, audit, drafts)

frontend/src/
├── App.jsx           # + 'social' view, renderSocial(), nav button
└── socialIssues.js   # (optional) issue label/copy constants
```

### Data flow

1. Operator enters niche + city on the Social tab, hits search.
2. `POST /api/social/search` runs the **existing** `GoogleMapsScraper` (same as
   `/api/search`), then for each business fetches its homepage with plain
   `httpx` (no Playwright, no `_PLAYWRIGHT_SEMAPHORE`) and regexes the
   `<a href>`s for `instagram.com` / `facebook.com` / `linkedin.com/company` /
   `youtube.com` URLs. Returns businesses each with a `handles` dict
   (`{instagram: url|None, youtube: url|None, ...}`).
3. Operator clicks "Audit socials" on a business (or Autopilot-style "audit all").
4. `POST /api/social/audit` (async_mode pattern, reusing the exact
   `_audit_progress`/`_audit_async_results`/`/progress`/`/result` shape from
   `/api/audit`) runs each detected platform's adapter, then
   `analyzer/social_audit.py` on each `SocialProfile`.
5. Result: per platform, a `SocialProfile` + ranked `SocialIssue[]` + generated
   `email` / `dm` / `whatsapp` copy.
6. Operator reviews, edits, and saves any of the three as a `social_drafts` row,
   or fires the assisted action (copy text + open channel).

## 5. Data model

### `SocialProfile` (`scrapers/social/base.py`)

Common shape every adapter returns (fields an adapter can't fill are left at
their zero value, never guessed):

```python
@dataclass
class SocialProfile:
    platform: str            # "instagram" | "youtube" | "facebook" | "linkedin"
    handle: str              # @name or channel/page slug
    url: str
    display_name: str = ""
    bio: str = ""
    followers: int = 0       # subscribers for YouTube, page likes for FB
    following: int = 0       # 0 where the platform doesn't expose it
    posts_count: int = 0
    posts_last_30_days: int = 0
    posting_frequency: str = ""      # reuse InstagramScraper._classify_frequency labels
    avg_engagement_rate: float = 0.0 # (likes+comments)/followers*100 where available
    uses_video: bool = False         # reels / shorts / native video
    has_link_in_bio: bool = False
    last_post_age_days: int | None = None
    sample_captions: list[str] = field(default_factory=list)
    analyzed: bool = True             # False -> adapter reached the profile but could not extract enough
    note: str = ""                   # why analyzed is False, shown in the UI
```

### `SocialIssue` (`analyzer/social_audit.py`)

```python
@dataclass
class SocialIssue:
    severity: str     # "high" | "medium" | "low"
    label: str        # short: "Inactive account"
    detail: str       # one sentence, plain language, no jargon, no dashes
```

### `social_drafts` table (`storage/db.py`, schema bump)

Mirrors `email_drafts`, plus platform/channel:

| column        | type    | notes |
|---------------|---------|-------|
| id            | INTEGER PK |
| timestamp     | TEXT default CURRENT_TIMESTAMP |
| company       | TEXT |
| platform      | TEXT    | instagram / youtube / facebook / linkedin |
| handle        | TEXT |
| profile_url   | TEXT |
| channel       | TEXT    | "email" / "dm" / "whatsapp" |
| target        | TEXT    | email address, @handle, or phone, depending on channel |
| subject       | TEXT    | "" for dm / whatsapp |
| body          | TEXT |
| issues_json   | TEXT    | JSON list of the SocialIssue dicts the copy was based on |

CRUD: `log_social_draft`, `get_social_drafts`, `delete_social_draft`,
`get_social_drafted_handles_summary` (for a "draft already made" badge later, out
of scope for v1). Schema version bump handled by the existing `PRAGMA
user_version` migration block.

## 6. Handle discovery

`scrapers/social/discover.py` — `find_social_handles(homepage_url) -> dict`.
Plain `httpx.AsyncClient` GET (5s timeout), BeautifulSoup over `<a href>`, first
match per platform wins:

- instagram: `instagram.com/<handle>` (exclude `/p/`, `/reel/`, `/explore`)
- youtube: `youtube.com/@<handle>` or `/channel/<id>` or `/c/<name>` or `/user/<name>`
- facebook: `facebook.com/<slug>` (exclude `/sharer`, `/dialog`, `/plugins`)
- linkedin: `linkedin.com/company/<slug>` (personal `/in/` profiles ignored — this
  is business outreach)

No match -> that platform is simply absent for that business. No platform search
by name in v1 (rate-limit and ban risk for FB/LinkedIn; deferred).

## 7. Issue detection (`analyzer/social_audit.py`)

Deterministic, platform-aware checks over a `SocialProfile`. Same philosophy as
`analyzer/flaws.py`: code decides the issues, the AI only writes copy about them.
`audit_profile(profile) -> list[SocialIssue]`:

| Check | Fires when | Severity |
|---|---|---|
| Inactive account | `posts_last_30_days == 0` or `last_post_age_days > 45` | high |
| Slowing cadence | `posting_frequency` in {"irregular", "weekly"} and followers > 1000 | medium |
| Low engagement | `avg_engagement_rate < 0.5` and followers > 500 (IG/FB only; not YouTube) | medium |
| Not using video | `uses_video is False` (reels for IG, shorts for YouTube) | medium |
| No link in bio | `has_link_in_bio is False` and platform in {instagram, facebook} | medium |
| Weak/empty bio | `len(bio.strip()) < 20` | low |
| Follower/following imbalance | platform == instagram and `following > followers` and `followers < 2000` | low |
| Thin catalogue | `posts_count < 9` | low |

Ranked high -> low. An unanalyzable profile (`analyzed is False`) yields **zero
issues** and a UI note, never a fabricated one.

## 8. Outreach copy generation

`analyzer/social_audit.py::generate_social_outreach(company, profile, issues,
channel, your_name) -> dict`. One AI call (same provider fallback chain and
`_AI_TEMPERATURE` as `AIAuditor`), reusing the MMGA cold-email formula already
built into `_build_prompt` (problem-first, one concrete issue, low-pressure CTA,
banned-generic-phrases rule). Channel shapes length/format:

- **email**: subject + 60-110 word body, cites the single most severe issue.
- **dm**: no subject, 2-3 sentences, casual, one issue, one soft CTA.
- **whatsapp**: no subject, 2-3 sentences, even more casual, emoji-light.

Grounding: the prompt is handed only the `SocialIssue` list — the model may not
introduce any metric or claim not in that list. A lightweight
`_check_number_hallucination`-style guard is reused if cheap; otherwise the
deterministic issue list is the whole ground truth and the prompt says so.

No `review_warnings` pipeline for v1 (that machinery is website-draft specific).
The operator reviews every draft inline before saving/sending, same as the
website audit view.

## 9. API routes (`app.py`)

All gated by `require_api_key` + `rate_limit`, same as every other route.

| Route | Method | Purpose |
|---|---|---|
| `/api/social/search` | POST | `{niche, city, limit, async_mode}` -> businesses with `handles` dict. Reuses `GoogleMapsScraper` + `find_social_handles`. Same async_mode/progress/result shape as `/api/search`. |
| `/api/social/search/progress` `/result` | GET | poll targets, mirror `/api/search/*` |
| `/api/social/audit` | POST | `{company, handles, platforms, async_mode}` -> per-platform `{profile, issues, outreach:{email,dm,whatsapp}}`. Mirrors `/api/audit` async_mode. |
| `/api/social/audit/progress` `/result` | GET | poll targets |
| `/api/social/drafts` | GET | list `social_drafts` rows |
| `/api/social/drafts` | POST | `{company, platform, handle, profile_url, channel, target, subject, body, issues}` -> save |
| `/api/social/drafts/{id}` | DELETE | remove |
| `/api/social/send` | POST | `{draft_id}` or `{company, target, subject, body}` -> real SES/Gmail send for a `channel == "email"` draft only. Thin wrapper over `emailer.get_sender().send_email` (no screenshot, no `review_warnings`, no staleness gate). DM and WhatsApp have **no** send route — assisted only. |

Cost logging: the one AI call per audited profile logs to `cost_logs` under a new
`"Social Audit"` category, same as `"AI Audit"`.

## 10. Frontend — Social tab (`frontend/src/App.jsx`)

New `currentView === 'social'`, nav button between "Agriculture" and "Drafts".
`renderSocial()`:

- **Search bar**: niche + city + limit, identical control to the Dashboard's,
  its own state (`socialNiche`, `socialCity`, ...). Submits to
  `/api/social/search` with `async_mode`, reuses the shared `pollSearchResult`
  helper.
- **Results grid**: one card per business — name, website, and a row of platform
  chips (Instagram / YouTube / Facebook / LinkedIn) that are solid when a handle
  was found, greyed when not. "Audit socials" button per card, plus a
  "Audit all" bulk runner paced like the existing Agriculture bulk runner,
  stoppable via a `socialStopRef`.
- **Audited card**: per platform, an expandable block —
  - profile summary (followers, cadence, engagement, last post age) or the
    "couldn't analyze this profile" note,
  - the ranked issue list (severity-coloured, same palette as budget badges),
  - three sub-tabs Email / DM / WhatsApp, each an editable subject (email only) +
    body, with **Copy**, **Open channel** (mailto: / instagram.com/direct or the
    profile / wa.me), and **Save draft** buttons.
- **Drafts section** (own, on this page — not the website Drafts tab): a list of
  `social_drafts` rows grouped Day/Week/Month/All (reuse `groupByPeriod` +
  `GroupBySelector`), each with Copy / Open / Delete. No "send" button for DM or
  WhatsApp (assisted only); email rows get a "Send" that posts to
  `/api/social/send` (real SES/Gmail send, thin wrapper, no screenshot / review
  pipeline / staleness gate).

  "Open channel" per row: email -> `mailto:` prefilled; instagram -> open
  `instagram.com/<handle>` (operator clicks Message there — no DM thread id to
  deep-link `direct/t/`); whatsapp -> `wa.me/<digits>?text=<body>`.

## 11. Config / new env vars

| Var | Purpose | Default |
|---|---|---|
| `YOUTUBE_API_KEY` | YouTube Data API v3 key (same Google Cloud project as `PAGESPEED_KEY` is fine; enable "YouTube Data API v3"). Unset -> the YouTube adapter is a silent no-op, same pattern as `mca_lookup`. | unset |

No new Python dependencies: YouTube API is a plain `httpx` GET against
`https://www.googleapis.com/youtube/v3/...`; FB/LinkedIn scrape reuses
`httpx` + `beautifulsoup4`, both already pinned.

## 12. Testing

- `test_social_discover.py` — `find_social_handles` against local HTML fixtures:
  each platform matched, `/p/` and `/in/` excluded, no-match -> absent, multiple
  links -> first wins.
- `test_social_audit.py` — `audit_profile` fires each issue on a crafted
  `SocialProfile` and stays silent on a healthy one; an `analyzed=False` profile
  yields zero issues; ranking order.
- `test_social_adapters.py` — YouTube adapter against a captured API JSON shape
  (mocked `httpx`); FB/LinkedIn adapters against captured HTML, and against a
  login-wall page returning `analyzed=False` not a crash; IG adapter maps an
  `InstagramData` to a `SocialProfile` correctly.
- `test_social_routes.py` — `TestClient`: every `/api/social/*` route is
  `require_api_key` + `rate_limit` gated; `social_drafts` CRUD round-trips;
  async_mode search/audit start/progress/result shape matches `/api/search`.
- `analyzer/social_audit.py` outreach generation: prompt contains the issue
  list and the no-new-claims instruction; a mocked provider returns parseable
  JSON; channel shapes differ.

## 13. Build order within this delivery

All four platforms ship together (operator's call), but internal order:

1. `SocialProfile` + `SocialAdapter` protocol + `analyzer/social_audit.py`
   (issue checks + outreach) with the IG adapter (wraps existing scraper).
2. `social_drafts` table + CRUD + `/api/social/drafts` routes.
3. `find_social_handles` + `/api/social/search`.
4. `/api/social/audit` async pipeline.
5. YouTube adapter (official API).
6. Facebook + LinkedIn best-effort adapters (return `analyzed=False` freely).
7. Frontend Social tab end to end.
8. Tests alongside each step (TDD per the repo convention).

## 14. Risks / open items

- **FB/LinkedIn breakage is expected, not exceptional.** The UI must make
  "couldn't analyze" a normal, unalarming state. Adapters must never raise into
  the pipeline — worst case `return None` / `analyzed=False`.
- **IG ban risk is unchanged** — this feature drives more IG traffic through the
  same burner account. The existing `_CHALLENGE_COOLDOWN_SECONDS` circuit
  breaker applies; a tripped breaker means the IG adapter returns `None` and the
  page shows the note. Consider a per-run IG profile cap.
- **YouTube quota**: 10,000 units/day; `channels.list` is ~1-3 units, `search` is
  100. Resolve a channel from a URL by ID/handle directly (cheap), avoid
  `search`. A niche run of 30 businesses is well under quota.
- **Handle discovery misses** businesses whose site doesn't link its socials.
  Acceptable for v1; platform-name search is the deferred follow-up.
- **No `review_warnings` for social copy** — lighter accuracy net than website
  drafts. Mitigated by the deterministic-issues-only grounding and mandatory
  inline review before save/send.
