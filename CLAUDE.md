# CLAUDE.md — Lead Audit Bot

> **MANDATORY**: Read this file in full before writing or modifying any code in this project.

---

## 1. Project Overview

**lead-audit-bot** is an automated lead-generation and audit pipeline that:

1. **Discovers** businesses via Google Maps / SerpAPI.
2. **Scrapes** their website, Instagram, and Apollo.io profiles.
3. **Enriches & scores** leads through a decision-maker module.
4. **Audits** their digital presence using AI (Gemini / OpenAI / Anthropic).
5. **Emails** personalized audit reports via AWS SES.
6. **Stores** all lead data in Google Sheets.
7. **Schedules** the entire pipeline via APScheduler.

---

## 2. Folder Structure

```
lead-audit-bot/
├── config.py                  # Env-var loader (python-dotenv)
├── main.py                    # Entry point
├── scheduler.py               # APScheduler job runner
├── scrapers/
│   ├── __init__.py
│   ├── google_maps.py         # GoogleMapsScraper — SerpAPI / Places API
│   ├── apollo.py              # ApolloScraper — Apollo.io REST API
│   ├── instagram.py           # InstagramScraper — instagrapi
│   └── website.py             # WebsiteScraper — httpx + BeautifulSoup
├── enrichment/
│   ├── __init__.py
│   └── decision_maker.py      # DecisionMaker — lead scoring (0-100)
├── analyzer/
│   ├── __init__.py
│   └── ai_audit.py            # AIAuditor — multi-provider AI reports
├── emailer/
│   ├── __init__.py
│   └── ses_sender.py          # SESSender — AWS SES with daily limit
├── storage/
│   ├── __init__.py
│   └── sheets.py              # SheetsStorage — gspread CRUD
├── requirements.txt
├── .env                       # Secret keys (never commit)
└── CLAUDE.md                  # ⬅ This file
```

---

## 3. Tech Stack & Dependencies

| Category        | Libraries                                  |
| --------------- | ------------------------------------------ |
| HTTP / Scraping | `httpx`, `beautifulsoup4`, `requests`, `playwright` |
| Instagram       | `instagrapi`                               |
| Search          | `serpapi`                                   |
| SEO             | `pyseoanalyzer`                            |
| AI Providers    | `google-generativeai`, `openai`, `anthropic` |
| Email           | `boto3` (AWS SES)                          |
| Storage         | `gspread`, `oauth2client`                  |
| Scheduling      | `apscheduler`                              |
| Data            | `pandas`                                   |
| Config          | `python-dotenv`                            |

All listed in `requirements.txt`. Install with: `pip install -r requirements.txt`

---

## 4. Configuration & Environment Variables

All env vars are loaded in `config.py` via `python-dotenv` from the `.env` file.

| Variable             | Required | Default       | Purpose                       |
| -------------------- | -------- | ------------- | ----------------------------- |
| `GEMINI_API_KEY`     | Yes      | —             | Google Gemini AI              |
| `OPENAI_API_KEY`     | Yes      | —             | OpenAI API                    |
| `ANTHROPIC_API_KEY`  | Yes      | —             | Anthropic Claude API          |
| `AWS_ACCESS_KEY`     | Yes      | —             | AWS IAM for SES               |
| `AWS_SECRET_KEY`     | Yes      | —             | AWS IAM for SES               |
| `AWS_REGION`         | No       | `ap-south-1`  | AWS region                    |
| `FROM_EMAIL`         | Yes      | —             | SES verified sender address   |
| `GOOGLE_SHEETS_ID`   | Yes      | —             | Target spreadsheet ID         |
| `GOOGLE_MAPS_API_KEY`| Yes      | —             | Google Maps / Places API      |
| `IG_USERNAME`        | Yes      | —             | Instagram login for instagrapi |
| `IG_PASSWORD`        | Yes      | —             | Instagram password for instagrapi |
| `PAGESPEED_KEY`      | No       | —             | Google PageSpeed Insights (25k/day free) |
| `APOLLO_API_KEY`     | No       | —             | Apollo Free API key for B2B scraping |
| `DAILY_EMAIL_LIMIT`  | No       | `100`         | Max emails per day            |
| `LEAD_SOURCE`        | No       | `maps`        | Scraper to use (`ecommerce`, `startups`, `b2b`, `maps`) |

**Rule**: Never hard-code secrets. Always use `config.<VAR_NAME>`.

---

## 5. Module Reference

### 5.1 `config.py`
- Calls `load_dotenv()` at import time.
- Exposes every env var as a module-level constant.
- `AWS_REGION` defaults to `"ap-south-1"`, `DAILY_EMAIL_LIMIT` defaults to `100`.

### 5.2 `main.py`
- Entry point (`python main.py`).
- Instantiates all core components: `DecisionMaker`, `InstagramScraper`, `WebsiteScraper`, `AIAuditor`, `SESSender`, `SheetsStorage`.
- `process_single_lead(lead) → str`: Runs a lead through the 7-step pipeline (enrich IG/contact, scrape IG, audit web, AI analysis, score gate < 70, generate + send email). Includes 30-90s delay between emails. Returns granular statuses: `"emailed"`, `"skipped"`, `"failed_no_email"`, `"failed_no_website"`, `"failed_ai_error"`, or `"failed_error"`.
- `run_batch()`: Queries SES quota, pulls pending leads from Sheets, runs pipeline until daily limit reached, and updates Sheets CRM.

### 5.3 `scheduler.py`
- Uses `apscheduler.schedulers.blocking.BlockingScheduler` configured for `"Asia/Kolkata"` timezone.
- `ingest_leads()`: Reads `config.LEAD_SOURCE`. Depending on the setting, it runs:
  - `ecommerce`: `ShopifyDorkScraper`
  - `startups`: `StartupDorkScraper`
  - `b2b`: `ApolloFreeScraper`
  - `maps`: `GoogleMapsScraper`
  Appends the results securely to Sheets.
- `start_scheduler()`: Registers the following cron jobs:
  - Weekdays (Mon-Fri) at 10:00 AM IST: `run_batch()` (from `main.py`)
  - Sundays at 8:00 PM IST: `ingest_leads()`

### 5.4 High-Value Scrapers

#### `scrapers/google_maps.py`
- `scrape_google_maps(niche, city)`. Uses Google Places API. Good for local businesses.

#### `scrapers/shopify_dork.py`
- `scrape(niche)`. Uses DuckDuckGo to run `site:myshopify.com` dorks. 100% free ecommerce scraping.

#### `scrapers/startup_dork.py`
- `scrape(niche)`. Uses DuckDuckGo to run `site:ycombinator.com/companies` dorks. 100% free funded startup scraping.

#### `scrapers/apollo_free.py`
- `scrape(niche)`. Hits `api.apollo.io/v1/mixed_people/search` for "Founder/CEO" titles. Requires free API key for 10k/month B2B leads.

### 5.5 `scrapers/instagram.py` — `InstagramScraper`
- **Status**: ✅ Functional
- `get_instagram_data(username) → InstagramData | None`
- Dataclass `InstagramData`: username, followers, following, posts_count, bio,
  posts_last_30_days, avg_likes, avg_comments, engagement_rate, uses_reels,
  has_link_in_bio, posting_frequency, sample_captions, content_types
- Session persistence: loads `session.json` or fresh login via `IG_USERNAME`/`IG_PASSWORD`
- Analyses last 20 posts: engagement rate, content-type breakdown (image/reels/carousel)
- Posting frequency: daily / 2-3x per week / weekly / irregular / inactive
- Anti-detection: random 3-5s sleep between scrapes
- Error handling: catches `LoginRequired`, `UserNotFound`, general exceptions → returns `None`
- Uses: `config.IG_USERNAME`, `config.IG_PASSWORD`

### 5.7 `scrapers/website.py` — `WebsiteScraper`
- **Status**: ✅ Functional (comprehensive auditor)
- `async audit_website(url) → WebsiteData`
- Dataclass `WebsiteData`: url, reachable, load_time_ms, page_speed_score, seo_score,
  mobile_score, has_cta, has_contact, has_testimonials, has_blog, has_ssl,
  meta_title, meta_description, h1_tags, homepage_text, issues
- **Step 1**: Reachability + load time (async httpx)
- **Step 2**: Google PageSpeed Insights (performance, SEO, mobile scores)
- **Step 3**: HTML parsing — CTA keywords, testimonial signals, blog links, phone/email regex
- **Step 4**: Plain-English issue generation for every failed check
- Returns `WebsiteData(reachable=False)` on unreachable sites
- Uses: `config.PAGESPEED_KEY`

### 5.7 `enrichment/decision_maker.py` — `DecisionMaker`
- **Status**: ✅ Complete
- `- instagrapi`: For extracting Instagram analytics (followers, engagement rate) and sending Auto-DMs.
- `send_dm(target_username, message) → bool`: Sends a direct message via Instagram (used for post-email follow-up).
- `- duckduckgo-search`: For finding Instagram handles and LinkedIn CEO discovery (OSINT) without a paid API.
- `- email-validator`: Verifies email existence via live SMTP checks to prevent bounces.
- `- trafilatura`: Extracts clean core text from messy HTML websites for the AI.
- `- python-Wappalyzer`: Profiles the website's technology stack (e.g. Shopify, React) for hyper-personalized AI emails.
- `- playwright`: Used for loading websites in a headless browser to capture mobile screenshots.
- `- Pillow`: Used for drawing visual evidence (red boxes) on the captured screenshots.
- `find_decision_maker(company_name, website) → dict` — Internal HTTP crawler finding `/contact` & `/about` pages, extracting emails using regex.
  - Returns: `{"name": str, "email": str, "title": str}`
  - Strategy: Regex crawler → LinkedIn DDG OSINT (CEO Discovery) → fallback to `marketing@`, `info@`, `hello@`
- `score_lead()` / `is_qualified()` — currently unused stubs (scoring is now handled dynamically via `AIAuditor` in `main.py` which skips scores > 70)
- Helper: `_extract_domain(website) → str` — strips www/protocol

### 5.8 `analyzer/ai_audit.py` — `AIAuditor`
- **Status**: ✅ Functional
- `analyze_lead(company, ig: InstagramData | None, web: WebsiteData) → dict | None`
- Returns: `{"flaws": [...], "overall_score": int, "email_subject": str, "opening_line": str}`
- Each flaw: `{"area": str, "headline": str, "detail": str, "impact": str}`
- Fallback chain: Gemini Flash (`gemini-2.0-flash`) → GPT-4o-mini → Claude Haiku (`claude-haiku-4-5-20251001`)
- `should_contact(audit_result) → bool` — True if `overall_score < 70`
- Prompt uses real IG + website data (engagement rate, page speed, issues, captions)
- JSON parser strips markdown fences and validates required keys
- Uses: `config.GEMINI_API_KEY`, `config.OPENAI_API_KEY`, `config.ANTHROPIC_API_KEY`
- Imports: `InstagramData` from `scrapers.instagram`, `WebsiteData` from `scrapers.website`

### 5.9 `emailer/ses_sender.py` — `SESSender`
- **Status**: ✅ Functional
- `generate_email(company, contact_name, analysis, your_name) → tuple[str, str]` — builds plain-text subject & body using AI analysis
- `send_email(to_email, subject, body, image_path=None) → bool` — sends email. If `image_path` provided, converts to `MIMEMultipart` raw email to embed visual evidence. Handles SES quotas and retries.
- `check_ses_quota() → dict` — returns SES API quota remaining.

### 5.9.1 `analyzer/visuals.py` (New)
- `generate_audit_screenshot(url, company_name) → str | None`: Headless playwright capturing a mobile view of the target site, and drawing a red audit box via Pillow for undeniable visual proof.

### 5.9.2 `warmup.py` (New)
- Standalone IMAP/SMTP script. Checks burner/seed inboxes for emails sent by the bot's domain, automatically opens them and flags them as important/not-spam to build robust domain reputation.

### 5.10 `storage/sheets.py` — `SheetsStorage`
- **Status**: ✅ Functional
- Requires `credentials.json` (Google service-account key) in project root.
- `init_sheet() → None` — populates headers if the sheet is empty
- `add_lead(data: dict) → None` — checks for existing email (deduplication) and appends a 17-column row
- `get_pending_leads() → list[dict]` — returns rows where Status is `pending` and Email is populated
- `get_stats() → dict` — returns aggregated counts by status (pending, emailed, skipped, etc.)
- `update_status(row_number: int, status: str) → None` — updates the Status column and the Sent At timestamp
- `mark_replied(row_number: int) → None` — directly sets a lead's Status to `replied`

---

## 6. Implementation Status

| Module                | Status          | Notes                                    |
| --------------------- | --------------- | ---------------------------------------- |
| `config.py`           | ✅ Complete     | All vars loaded                          |
| `main.py`             | ✅ Complete     | Pipeline orchestration: pull leads, scrape, enrich, audit, email, update CRM |
| `scheduler.py`        | ✅ Complete     | Cron jobs for ingestion (Sun) and emails (Mon-Fri) |
| `google_maps.py`      | ✅ Complete     | Places Text Search + Details + dedup     |
| `instagram.py`        | ✅ Complete     | instagrapi + session + engagement analytics |
| `website.py`          | ✅ Complete     | Full 4-step audit: reachability + PageSpeed + HTML + issues |
| `decision_maker.py`   | ✅ Complete     | IG handle + decision-maker lookup |
| `ai_audit.py`         | ✅ Complete     | Gemini → GPT-4o-mini → Claude Haiku fallback |
| `ses_sender.py`       | ✅ Complete     | SES template generation, retry logic, and quota checks |
| `sheets.py`           | ✅ Complete     | Google Sheets CRM: dedup, queries, update methods |

---

## 7. Coding Rules

1. **Always import config** — use `import config` then `config.VAR_NAME`. Never use `os.getenv()` directly outside `config.py`.
2. **One class per module** — each module exposes a single primary class.
3. **Type hints** — use `list[dict]`, `str`, `int`, `bool` etc. on all public method signatures.
4. **Docstrings** — every class and public method must have a docstring describing params and return value.
5. **Error handling** — raise descriptive exceptions. Use `RuntimeError` for operational failures, `ValueError` for bad input.
6. **No hard-coded secrets** — all keys/credentials come from `config.py`.
7. **No print-based logging in modules** — use `print()` only in `main.py` for now. Modules should return data or raise exceptions.
8. **Keep stubs consistent** — unimplemented methods raise `NotImplementedError` with no message.
9. **HTTP clients** — use `httpx` (sync) for scraping. Use `requests` only if a third-party library requires it.
10. **Imports** — stdlib first, then third-party, then local (`config`, other modules). Blank line between groups.

---

## 8. Data Flow (Target Architecture)

```
┌─────────────┐                      ┌──────────────┐
│ Google Maps  │                     │  Instagram   │
│  (discover)  │                     │  (social)    │
└──────┬───────┘                     └──────┬───────┘
       │                                    │
       ▼                                    ▼
┌─────────────────────────────────────────────────────────┐
│                  WebsiteScraper                         │
│             (fetch HTML + extract metadata)             │
└─────────────────────────┬───────────────────────────────┘
                          │
                          ▼
              ┌───────────────────────┐
              │    DecisionMaker      │
              │  (enrich contact/IG)  │
              └───────────┬───────────┘
                          │
                          ▼
              ┌───────────────────────┐
              │      AIAuditor        │
              │ (generate audit report│
              │  & filter score < 70) │
              └───────────┬───────────┘
                          │ qualified leads only
                ┌─────────┴─────────┐
                ▼                   ▼
      ┌──────────────┐    ┌──────────────┐
      │  SESSender   │    │ SheetsStorage│
      │ (email audit)│    │ (log lead)   │
      └──────────────┘    └──────────────┘
```

---

## 9. How To Run

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Fill in .env with your API keys

# 3. Place Google service-account credentials.json in project root

# 4. Run
python main.py
```

---

## 10. Changelog

| Date       | Change                                              |
| ---------- | --------------------------------------------------- |
| 2026-06-24 | Initial project scaffold created — all files, stubs, config, requirements. |
| 2026-06-24 | `google_maps.py` fully implemented — Text Search + Details + pagination + dedup. |
| 2026-06-24 | `apollo.py` fully implemented — People Search + pagination + rate-limit retry. |
| 2026-06-24 | `decision_maker.py` — `find_instagram_handle` + `find_decision_maker` implemented. Added `SERPAPI_KEY` + `HUNTER_API_KEY` to config/.env. |
| 2026-06-24 | `instagram.py` fully implemented — instagrapi + InstagramData dataclass + engagement analytics. Added `IG_USERNAME` + `IG_PASSWORD` to config/.env. |
| 2026-06-24 | `website.py` rewritten as full auditor — WebsiteData dataclass, 4-step audit (reachability, PageSpeed, HTML parsing, issues). Added `PAGESPEED_KEY` to config/.env. |
| 2026-06-24 | `ai_audit.py` fully implemented — Gemini → GPT-4o-mini → Claude Haiku fallback chain, data-rich prompt, JSON parser, contact threshold. |
| 2026-06-24 | `ses_sender.py` rewritten to use `boto3`, plain-text generation, retry logic, and SES quota checking. |
| 2026-06-24 | `sheets.py` fully implemented — Google Sheets CRM using `gspread` with dedup, queries, and status updates. |
| 2026-06-24 | `main.py` fully implemented — orchestrates the entire pipeline from Google Sheets -> Scraping -> AI Audit -> SES. |
| 2026-06-24 | `scheduler.py` fully implemented — APScheduler with Sun ingestion and Mon-Fri batch sending. |
| 2026-07-06 | Removed Apollo, SerpAPI, and Hunter dependencies to shift bot to $0/month cost. Implemented DuckDuckGo and regex web crawling for discovery. |
| 2026-07-06 | Implemented God-Tier outreach features: Wappalyzer tech profiling, LinkedIn OSINT (CEO Discovery), Playwright + Pillow for visual evidence screenshots, MIMEMultipart embedded emails, Instagram Auto-DMs, and a standalone Email Warmup script. |

---

> **Reminder**: Update this file whenever you add new modules, change the data flow, or modify coding conventions.
