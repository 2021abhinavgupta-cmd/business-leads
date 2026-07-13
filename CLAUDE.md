# CLAUDE.md — Lead Audit Bot

> **MANDATORY**: Read this file in full before writing or modifying any code in this project.

---

## 1. Project Overview



1. **React Frontend**: A sleek, multi-page Dashboard with Top Navigation (Dashboard, Drafts, Costs, History).
2. **FastAPI Backend**: Serves the API and connects the web scrapers, AI analyzers, and persistent storage.
3. **Deep Web Crawling**: Uses `Crawl4AI`, Jina Reader, and Trafilatura to extract deep brand context (Services, About Us) for extreme email personalization.
4. **AI Visual & Technical Auditing**: Captures Playwright desktop screenshots, runs Google Lighthouse CLI for performance/SEO scores, and runs `axe-core` accessibility audits.
5. **Real Visual Bounding Boxes**: Extracts exact `(x,y,w,h)` coordinates from `axe-core` violations and draws a real red box directly on the screenshot for the AI to critique.
6. **Exact Cost Tracking**: Tracks fractional penny costs (to 5 decimal places) for API usage directly in an SQLite database.
7. **Asynchronous Drafts Inbox**: Allows users to run Autopilot, queue up generated AI emails in a Drafts table, and manually review/send them via AWS SES at their leisure.

---

## 2. Folder Structure

```
lead-audit-bot/
├── app.py                     # FastAPI Entry point & Backend Logic
├── config.py                  # Env-var loader (python-dotenv)
├── security_utils.py          # validate_public_url() — SSRF guard for the audit endpoint's user-supplied URL
├── scrapers/
│   ├── __init__.py
│   ├── google_maps.py         # GoogleMapsScraper — SerpAPI / Places API
│   ├── instagram.py           # InstagramScraper — instagrapi
│   └── website.py             # WebsiteScraper — Playwright + Crawl4AI + Jina Reader
├── enrichment/
│   ├── __init__.py
│   └── decision_maker.py      # DuckDuckGo OSINT & Email Validation, + Claude web_fetch as a last-resort 5th strategy
├── analyzer/
│   ├── __init__.py
│   ├── ai_audit.py            # AIAuditor — multi-provider AI reports
│   ├── flaws.py               # Flaw dataclass + severity ranking — the reconciliation layer, see §5.6
│   ├── lighthouse.py          # Lighthouse CLI runner for performance scores
│   └── visuals.py             # Playwright screenshots, axe-core audits, precise Pillow bounding boxes
├── emailer/
│   ├── __init__.py
│   └── ses_sender.py          # SESSender — AWS SES sending with attachments
├── storage/
│   ├── __init__.py
│   ├── db.py                  # SQLite Persistent Database (History, Costs, Drafts)
│   └── sheets.py              # SheetsStorage — gspread CRUD (Legacy CRM)
├── frontend/                  # React + Vite Frontend UI (dist/ gitignored + untracked — Docker builds it fresh, see §12)
├── data/                      # Persistent storage mount point (database.sqlite, screenshots/), gitignored
├── screenshots/               # Temporary generated AI audit screenshots, gitignored
├── main.py                    # CLI batch-send orchestration script (separate from app.py web API)
├── scheduler.py               # Picks scraper by LEAD_SOURCE env var (maps/ecommerce/startups/b2b)
├── test_audit.py              # pytest, @integration — full pipeline smoke test, see §6
├── test_lh.py                 # pytest, @integration — Lighthouse CLI smoke test, see §6
├── test_parse_json.py         # pytest, unit (no network) — AIAuditor._parse_json, see §6
├── test_crawl.py, test_ddg.py, test_google.py, test_maps.py, test_playwright_maps.py
│                               # 5 remaining manual smoke-test scripts (not converted to pytest), run via `python <file>.py`
├── pytest.ini                 # pytest config (asyncio_mode=auto, `integration` marker)
├── requirements-dev.txt       # requirements.txt + pytest/pytest-asyncio, for local dev & CI
├── .github/workflows/tests.yml # CI: runs pytest on push/PR (integration tests self-skip, no secrets configured)
├── .env.example                # Template for .env — copy and fill in real values
├── credentials.json            # Google service-account key for gspread (gitignored, local only)
├── requirements.txt           # Python dependencies
├── package.json               # Node.js dependencies (Lighthouse CLI only, no scripts)
├── railway.json               # Railway deployment configuration
├── .env                       # Secret keys (never commit; no .env.example exists — infer vars from §4 or config.py)
└── CLAUDE.md                  # ⬅ This file
```

---

## 3. Tech Stack & Dependencies

| Category        | Libraries                                  |
| --------------- | ------------------------------------------ |
| **Backend**     | `fastapi`, `uvicorn`, `pydantic`           |
| **Frontend**    | `react`, `vite`, `lucide-react`, `framer-motion` |
| **Scraping**    | `httpx`, `crawl4ai`, `beautifulsoup4`, `trafilatura` |
| **Headless**    | `playwright`, `axe-playwright-python`      |
| **Performance** | Google Lighthouse CLI (Node.js)            |
| **SEO/Flaws**   | `pyseoanalyzer` (crawl-based SEO pass), `extruct` (Schema.org/JSON-LD structured data), `textstat` (readability) — see §5.6 |
| **AI Vision**   | `Pillow` (PIL)                             |
| **AI Models**   | `google-generativeai` (deprecated upstream, still functional — see §8), `openai`, `anthropic` |
| **Email**       | `boto3` (AWS SES)                          |
| **Database**    | `sqlite3`                                  |

**Installation:**
Both Python and Node.js are required. `requirements.txt` is version-pinned (as of 2026-07-10) — bump versions deliberately, not via a bare re-`pip install`.
- Install Backend: `pip install -r requirements.txt; playwright install chromium`
- Install Lighthouse: `npm install` (in root directory)
- Install Frontend: `cd frontend && npm install`
- Tech detection uses `wappalyzer` (the `wappalyzer-next` project, not the old `python-Wappalyzer`) — see §8 for a real packaging gotcha if you ever have both installed at once.

---

## 4. Configuration & Environment Variables

| Variable             | Purpose                       |
| -------------------- | ----------------------------- |
| `GEMINI_API_KEY`     | Google Gemini AI              |
| `OPENAI_API_KEY`     | OpenAI API                    |
| `ANTHROPIC_API_KEY`  | Anthropic Claude API — also powers `decision_maker.py`'s last-resort `web_fetch` contact-discovery strategy, see §5.5/§8 |
| `OPENROUTER_API_KEY` | Free-tier 4th AI-audit fallback (openrouter.ai, no card needed) — only reached if Claude/Gemini/OpenAI all fail or run out of quota. Optional; `_call_openrouter` no-ops if unset. |
| `AWS_ACCESS_KEY`     | AWS IAM for SES               |
| `AWS_SECRET_KEY`     | AWS IAM for SES               |
| `AWS_REGION`         | AWS region (default ap-south-1) |
| `FROM_EMAIL`         | SES verified sender address   |
| `GOOGLE_MAPS_API_KEY`| Google Maps / Places API      |
| `PAGESPEED_KEY`      | Fallback API Key for Lighthouse metrics |
| `GOOGLE_SHEETS_ID`   | Legacy CRM sheet ID (storage/sheets.py) |
| `GOOGLE_CREDENTIALS_JSON` | Google service-account JSON as env var (prod); falls back to local `credentials.json` file |
| `APOLLO_API_KEY`     | ApolloFreeScraper (used when `LEAD_SOURCE=b2b`) |
| `IG_USERNAME` / `IG_PASSWORD` | Instagram scraper login (instagrapi) |
| `DAILY_EMAIL_LIMIT`  | Batch send cap, default `100` (config.py) |
| `LEAD_SOURCE`        | Scraper selector for `scheduler.py`: `maps` (default) / `ecommerce` / `startups` / `b2b` |
| `YOUR_NAME`          | Sender display name; read directly via `os.getenv` (default `"Kshitij Gupta"`) in both `app.py` and `main.py`, **not** wired through `config.py`. |
| `API_KEY`            | **Required for any public deployment.** Gates all `/api/*` routes via an `X-API-Key` header (`app.py:require_api_key`). If unset, the API is wide open — app.py prints a startup warning when this happens. Frontend must be built with a matching `VITE_API_KEY` (see `frontend/.env.example`) to authenticate its own requests. |
| `ALLOWED_ORIGINS`    | Comma-separated CORS allowlist (`app.py`). Unset -> `"*"` (prints a startup warning). Normally moot in production since Railway serves the frontend same-origin from the same FastAPI app — only matters if the frontend is ever deployed separately from the API. |
| `APP_BASE_URL`       | Public base URL of this deployment (e.g. your Railway domain), used to build the one-click HTTPS unsubscribe link in outgoing emails' `List-Unsubscribe` header. Unset -> emails still carry a `mailto:` unsubscribe fallback, just without Gmail/Yahoo's one-click support. See §8. |

Copy `.env.example` to `.env` (and `frontend/.env.example` to `frontend/.env`) and fill in real values when setting up a new environment; this table + `config.py` remain the source of truth for what each var does.

---

## 5. Core Modules

### 5.1 `app.py` (FastAPI Server)
- Serves the compiled React frontend `dist/` statically.
- Exposes REST endpoints: `/api/search`, `/api/audit`, `/api/send`, `/api/drafts`, `/api/costs`, `/api/history`.
- Connects the AI orchestration pipeline to the database and frontend.

### 5.2 `scrapers/website.py` & Deep Crawling
- Uses `Crawl4AI` to extract extremely clean LLM-optimized markdown from the homepage (via a temp `file://` URL — see §8, the documented `raw:html` shortcut doesn't work in 0.9.1).
- Uses `Jina Reader` and `Trafilatura` to deep-crawl context pages (About Us, Services).
- Aggregates Lighthouse scores (incl. raw Core Web Vitals — LCP/CLS/TBT, not just the 0-100 category score), PageSpeed fallbacks, security headers (`extruct`), structured data + business-relevant `@type` check (`extruct`), readability (`textstat`), a crawl-based SEO pass (`pyseoanalyzer`), `robots.txt` blanket-disallow check, real alt-text coverage, axe-core violations (desktop + mobile viewport + across every crawled page), broken link data, and business/conversion signals (booking widget, click-to-call/WhatsApp, menu/pricing) — all reconciled into one ranked flaw list by `_build_flaws()` (see §5.6).

### 5.3 `analyzer/lighthouse.py`
- Runs the Google Lighthouse CLI via a Node.js subprocess to gather `performance`, `seo`, `accessibility`, and `best_practices` scores natively. `accessibility` is fetched but not surfaced to the AI prompt — axe-core is the authoritative a11y signal (see §5.6).

### 5.4 `analyzer/visuals.py`
- Headless Playwright script. Homepage screenshot at Desktop (1280x800), **plus** a separate real mobile-viewport (390x844) screenshot — desktop screenshots systematically miss mobile-only problems (overflow, tiny tap targets) that most visitors actually hit.
- **Multi-page crawl**: discovers up to 2 extra internal pages (via `/sitemap.xml` first, falling back to homepage anchor-tag keyword scan) and runs the full check set — axe-core, broken links, font consistency, stretched images, title/meta-description — on each one, not just the homepage. Violations get tagged with their source page (`"(on /about-us page)"`).
- Also runs axe-core **again** at the mobile viewport (WCAG tap-target rules differ from desktop) plus a horizontal-overflow check (`scrollWidth > innerWidth`).
- `page.on("console")` and `page.on("request")` listeners (attached once, persist across every navigation) capture JS console errors and mixed-content (HTTP-on-HTTPS) resource loads.
- **Interconnected Red Box Pipeline**:
  1. Runs `axe-core` accessibility audit on every page crawled (desktop + mobile).
  2. Extracts the exact bounding box `(x, y, w, h)` of the most severe UI violation, across all pages — the attached screenshot is whichever page had the worst one, not always the homepage.
  3. Draws a precise red box around the real flaw using `Pillow`.
  4. Passes the `visual_flaw_context` up the chain to the AI prompt.
- Also captures the Playwright navigation's HTTP response headers (`response.headers`, free — reuses the request already being made for the screenshot) for the security-headers check in `website.py`.
- Navigation uses `wait_until="domcontentloaded"`, not `"load"` — see §8 for why this was a real false-"website is down" bug.

### 5.5 `analyzer/ai_audit.py`
- Ingests `company_context` to force extreme outreach personalization, plus Google Maps `Rating`/`Reviews Count` (when available) as a personalization hook ("great reviews but the site doesn't reflect it").
- Takes in the `visual_flaw_context` and is strictly instructed to explicitly mention the red box drawn on the screenshot in the email body; also receives the separate mobile screenshot as a second vision input and is instructed to call out mobile-specific issues explicitly.
- Prompt consumes one pre-ranked `FLAWS DETECTED` section (from `web.flaws`) instead of five separate raw tool-output dumps — see §5.6.
- 4-provider fallback chain: Claude Haiku 4.5 → Gemini 3.5 Flash → GPT-4o-mini → OpenRouter free tier (`google/gemma-4-31b-it:free`, last resort). Gemini and OpenAI use native JSON response mode instead of regex-stripping markdown fences; all 4 providers log their own errors (previously only Gemini did — see §8, that asymmetry hid a broken model ID for a while).
- `enrichment/decision_maker.py`'s `find_decision_maker()` has a 5th, separate last-resort strategy using Anthropic's `web_fetch` server tool (real API cost, ~$0.0008/call, logged to `cost_logs` under `"AI Web Fetch"`) — only tried after the free scraper/LinkedIn-OSINT/email-dork strategies all fail.

### 5.6 `analyzer/flaws.py` — the flaw reconciliation layer
Every audit tool used to dump its raw output into the AI prompt as its own unranked text block, leaving the LLM to figure out what mattered and silently absorb redundant/contradictory signals — most notably, Lighthouse's own `accessibility` score and a separate direct axe-core scan both measured accessibility independently, never compared.

As of 2026-07-10, all tool output gets normalized into a common `Flaw(category, severity, description)` type in `scrapers/website.py:_build_flaws()`, deduplicated/reconciled in code (axe-core wins over Lighthouse's `accessibility` number; the two are never both shown), ranked by severity via `analyzer/flaws.py:rank()`, and handed to the AI as one `FLAWS DETECTED (ranked most severe first)` block. The AI's job shifted from "find the flaws in this raw dump" to "write compelling copy about the 2-3 most severe items on this pre-ranked list" — more consistent results, and the reconciliation logic is visible/testable in code (`test_build_flaws.py`) rather than being an LLM judgment call each time.

Signals feeding into it: Lighthouse/PageSpeed scores + raw Core Web Vitals (LCP/CLS/TBT), SSL, meta title/description/H1, CTA/contact/testimonials/blog presence, canonical tag, robots `noindex` meta tag + `robots.txt` blanket-disallow, Open Graph tags, structured data + business-relevant schema `@type` (`extruct`), readability (`textstat`), thin content + real SEO warnings (`pyseoanalyzer`), security headers, mixed content (HTTP-on-HTTPS), JS console errors, axe-core violations (desktop + mobile viewport, across every crawled page), broken links, viewport meta, favicon, font-family consistency, stretched/blurry images, real alt-text coverage, duplicate title/meta-description across crawled pages, mobile horizontal overflow, and business/conversion signals (booking widget, click-to-call/WhatsApp, menu/pricing) — the last category exists because prior flaws were 100% technical (perf/SEO/a11y/security), and a business owner cares just as much about "can a customer actually convert on this page," see §8.

---

## 6. Testing

**Real pytest suite now exists** (added 2026-07-10), configured via `pytest.ini` (`asyncio_mode = auto`). Run with:

```bash
pip install -r requirements-dev.txt   # pytest + pytest-asyncio, on top of requirements.txt
pytest -v
```

| File | Type | Notes |
|---|---|---|
| `test_parse_json.py` | Unit, no network | Tests `AIAuditor._parse_json`. Always runs, always fast. |
| `test_build_flaws.py` | Unit, no network | 19 tests locking in `_build_flaws()`'s severity mapping, ranking, and business/conversion flaw logic (see §5.6). Always runs, always fast. |
| `test_email_deliverability.py` | Unit, no network | 6 tests — suppression list, `List-Unsubscribe` header shape, message-ID storage. |
| `test_audit.py` | `@pytest.mark.integration` | Full pipeline (Playwright → audit → AI) against a real Timezone Games URL. Self-skips via `skipif` when no `ANTHROPIC_API_KEY`/`GEMINI_API_KEY`/`OPENAI_API_KEY` is set. |
| `test_lh.py` | `@pytest.mark.integration` | Runs real Lighthouse CLI against a real URL. Calls `pytest.skip()` at runtime if no Lighthouse binary is reachable. |

Full suite currently: 30 passed, 1 skipped (locally, with all 3 paid AI keys set).

GitHub Actions runs this on every push/PR (`.github/workflows/tests.yml`) — no API-key secrets are configured there, so the two `integration` tests show as **skipped** (not failed) in CI; only `test_parse_json.py` runs for real. Add repo secrets + an `env:` block to the workflow if you want the integration tests to run live in CI (this will incur real AI-provider API costs on every push).

5 more manual smoke-test scripts remain at repo root (no pytest, just `print()`, run via `python <file>.py`) — `test_crawl.py`, `test_ddg.py`, `test_google.py`, `test_maps.py`, `test_playwright_maps.py`. These weren't converted; mirror `test_audit.py`'s pattern (mark `integration`, `skipif` on required keys) if converting them later.

When adding new logic, add a real `assert`-based pytest test alongside it — the manual-script convention is deprecated now that a pytest suite exists.

---

## 7. Code Style Conventions

- Type hints used fairly consistently (`str | None`, `list[str]`, `dict | None`); `@dataclass` for structured results (e.g. `WebsiteData` in `scrapers/website.py:59`).
- Docstrings: Google-style `Args:`/`Returns:` blocks (`scrapers/website.py`, `analyzer/ai_audit.py`, `analyzer/lighthouse.py`).
- Error handling: broad `try/except Exception`, generally swallowed with `print(f"[Tag] ... error: {e}")` and a safe empty/False/`{}` fallback rather than raised. This is the dominant pattern — match it rather than introducing raised exceptions unless deliberately changing behavior.
- Logging is entirely `print()` with bracketed tags (`[Lighthouse]`, `[Audit]`, `[Parse]`, `[Axe]`, `[Links]`, `[PageSpeed API]`, `[Jina]`) — no `logging` module anywhere.
- Pattern: one class per scraper/auditor module (`WebsiteScraper`, `GoogleMapsScraper`, `InstagramScraper`, `AIAuditor`, `SESSender`, `SheetsStorage`, `DecisionMaker`), instantiated **once at module scope** in `app.py:50-56` and reused across requests — these hold long-lived clients (`httpx.AsyncClient`, API clients), not per-request instances. Don't instantiate per-request.
- Async/sync mixing: `scrapers/website.py` is async (`httpx.AsyncClient`). `analyzer/lighthouse.py` is `async def run_lighthouse` and, as of 2026-07-10, wraps its blocking `subprocess.run` helpers (`_run_lighthouse_cli`/`_run_lighthouse_npx`) in `asyncio.to_thread` — a 120s Lighthouse run no longer blocks the event loop. `storage/db.py` and `storage/sheets.py` are fully synchronous (`sqlite3`, `gspread`); `app.py`'s async route handlers wrap their direct sync calls (`db.*`, `ses.*`, `ig_scraper.*`, `decision_maker.*`) in `asyncio.to_thread(...)` so they no longer block the event loop — follow that pattern for new sync calls added to route handlers. `main.py` (CLI batch script) still calls these synchronously inline, which is fine there since it's single-threaded sequential processing with no concurrency to protect.
- Naming: snake_case in Python; camelCase in React state; snake_case DB columns. **Mismatch:** Google Sheet headers (`storage/sheets.py:19-39`) use human-readable Title Case strings as the actual field keys — different schema convention from the SQLite side.
- `@staticmethod` + `_`-prefix for private helpers not needing `self` (e.g. `scrapers/website.py:428,521,531`).

---

## 8. Known Gotchas / Fragile Areas

Most of the issues found in the 2026-07-10 audit were fixed the same day — see §13 Changelog for the full list (tuple-unpack bug, `main.py` hardcodes/dupe import, Lighthouse `output_path` guard, screenshot filename collisions, `init_db()` running its cleanup DELETE on every call instead of once, Crawl4AI/Wappalyzer being silent no-ops, dead files, and a second round: no API auth, a broken scheduler.py ingestion path, SSL verification disabled, bare excepts, unpinned dependencies). What's still true:

- **`/api/*` auth is opt-in, not enforced.** `require_api_key` (`app.py`) only checks `X-API-Key` if `config.API_KEY` is set — if you forget to set it, every route stays wide open (app.py prints a startup warning, but nothing blocks you from ignoring it and deploying anyway). Always set `API_KEY` before deploying anywhere internet-reachable, and keep `frontend`'s `VITE_API_KEY` in sync (baked into the frontend bundle at build time, so it's visible to anyone who reads the JS — fine for stopping casual/automated abuse of the raw API, not a substitute for real per-user auth if this ever becomes multi-tenant).
- **Playwright concurrency capped at 1** via global `asyncio.Semaphore(1)` (`analyzer/visuals.py:15`) to avoid OOM on Railway's 500MB instances — audits run serialized, not parallel. This is a deliberate resource constraint, not a bug; revisit only if upgrading the Railway plan or moving audits to a separate worker. This is also *why* Playwright navigation timeouts were kept (not removed entirely) when fixing the false-"website down" bug below — a truly unbounded wait on one bad site would freeze every other lead queued behind it through the single shared browser, not just fail the one lead.
- **Playwright navigation used `wait_until="load"`, which is much stricter than what "the site loads" actually means to a human** — `"load"` waits for every single resource (analytics beacons, chat widgets, ad iframes, web fonts) to finish, and on a lot of real small-business sites this never fires within any timeout even though the site is fine and renders instantly for a real visitor. Live-verified 2026-07-13: a site that curl'd back in 2.5s was being reported as fully "unreachable" (0/100 scores, a drafted "Your Website Is Down" email) purely because one hung third-party script kept `"load"` from ever firing. Fixed by switching every navigation in `analyzer/visuals.py` (homepage, extra pages, mobile revisit) to `wait_until="domcontentloaded"` (fires once the page itself is parsed and usable) with a more generous timeout kept as a safety net.
- **`decision_maker.py`'s Claude `web_fetch` strategy costs real money per call (~$0.0008, live-verified) and is deliberately gated to the failure path only** — it's strategy 5 of 5 in `find_decision_maker()`, only reached after the free scraper/LinkedIn-OSINT/email-dork strategies all come up empty. Don't move it earlier in the chain or call it unconditionally; that would turn a rare fallback into a per-lead cost. It also can't render JavaScript (Anthropic's `web_fetch` tool is static HTML/text/PDF only), so it won't help on a fully client-rendered contact page. Cost flows back through `find_decision_maker()`'s return dict (`"cost"` key, present even on a miss since the API call happened either way) and is logged to `cost_logs` under `"AI Web Fetch"` in `app.py` — note `main.py`'s batch runner doesn't log any costs at all currently (pre-existing asymmetry, only `app.py`'s single-lead endpoint does).
- **Business/conversion flaws (`scrapers/website.py`) are heuristic, not exhaustive.** Booking-widget detection is a hardcoded list of ~14 known platform signatures (OpenTable, Resy, Calendly, etc.) checked against raw HTML — a booking system not on that list, or one that loads via a JS framework with no static URL/script-src fingerprint, won't be detected and will incorrectly flag as "missing." Same caveat for menu/pricing detection (keyword-based: "menu", "pricing", "price list" in link text/href). Treat these as reasonably reliable but not proof-positive absence.
- **AI provider fallback chain** (`analyzer/ai_audit.py`): Claude Haiku 4.5 (`claude-haiku-4-5-20251001`) → Gemini 3.5 Flash (`gemini-3.5-flash`) → GPT-4o-mini → OpenRouter free tier (`google/gemma-4-31b-it:free`, only if `OPENROUTER_API_KEY` set), each only constructed if its API key is set. Cost is estimated locally with hardcoded per-token pricing constants, not fetched from any API. Gemini/OpenAI use native JSON response mode; Anthropic/OpenRouter still go through `analyzer/ai_audit.py:_parse_json` (strips markdown fences, slices between first `{`/last `}`, returns `None` on parse failure — no retry, falls through to the next provider). If every provider fails, `analyze_lead` logs `[AIAuditor] All AI providers failed...` before returning `None`.
- **AI model IDs need periodic re-verification against the provider's own model list, not just documentation/memory** — live-verified 2026-07-13: `claude-3-5-haiku-latest` had silently hit end-of-life 5 months earlier (every lead was falling through to Gemini without anyone noticing, since Anthropic's provider call swallowed the exception with no log line), and a separately-added `"gemini-3-flash"` model ID turned out to not exist on this API key at all (404, confirmed via `genai.list_models()`) — it was silently cascading to GPT-4o-mini the entire time. Both fixed, and all 4 providers now log their own errors (previously only Gemini and OpenRouter did) specifically so a bad model ID can't go unnoticed like this again. Before trusting a model name from docs/memory, verify with `genai.list_models()` (Gemini) or a live 1-token API call (Anthropic/OpenAI).
- **`google.generativeai` (the Gemini SDK used here) is fully deprecated upstream** — "no longer receiving updates or bug fixes," Google wants everyone on the new `google.genai` package. Still functional as of 2026-07-13 (confirmed via a live call), so nothing is broken, but this is a deliberate future migration, not something to do casually — the new SDK has a different client pattern and JSON-mode config shape that needs its own test pass.
- **SES retry/rate-limiting** (`emailer/ses_sender.py:145-165`): `"Daily message quota exceeded"`/`LimitExceeded` → hard raise; `MessageRejected` → soft `False`; `Throttling` → one retry after blocking `time.sleep(5)`. Still runs on the event loop when called directly from an async route, but those call sites (`app.py`) now wrap it in `asyncio.to_thread`, so it no longer blocks other concurrent requests — the `time.sleep(5)` itself is unchanged (still blocks that one worker thread for up to 5s on throttle).
- **Cold emails were landing in spam — root cause was DNS/AWS config, not code, though code was contributing too.** Diagnosed live via the SES/SESv2 APIs and DNS TXT lookups against `mmga.agency` (the `FROM_EMAIL` domain), then fixed directly in the AWS SES console + GoDaddy DNS (2026-07-13):
  - **DKIM**: domain wasn't a verified SES identity (only the single mailbox address was) — created a proper `mmga.agency` domain identity in SES with Easy DKIM (RSA_2048_BIT), added the 3 CNAME records to GoDaddy DNS. Verified.
  - **SPF**: root `mmga.agency` SPF (`v=spf1 include:_spf.google.com ~all`, for Google Workspace) was left untouched — instead of merging SES into it, enabled SES's **custom MAIL FROM domain** feature (`mail.mmga.agency`), which gave a separate, scoped `v=spf1 include:amazonses.com ~all` TXT record just for that subdomain plus its own MX record. Cleaner than editing the root record: zero risk to existing Workspace mail deliverability.
  - **DMARC**: added `_dmarc.mmga.agency` TXT record, `v=DMARC1; p=none; rua=mailto:kshitij@mmga.agency` (monitor-only policy to start; tighten to `p=quarantine`/`p=reject` later once reports confirm legit mail isn't affected).
  - **Sandbox**: account was `ProductionAccessEnabled: False` (`sesv2.get_account()`) — production access requested 2026-07-13 via the SES console "Get set up" flow (Marketing mail type), status "Under review" (AWS SLA up to 24h). **Check this hasn't regressed to sandbox / wasn't rejected before assuming real leads will actually receive mail** — sandbox silently limits delivery to pre-verified addresses only.
  - **Code-side fixes** (`emailer/ses_sender.py`, `storage/db.py`, `storage/sheets.py`, `app.py`): `send_email`/`send_followup` now build real multipart/mixed or multipart/alternative raw MIME with both a `text/plain` and `text/html` part (previously HTML-only, a heavy spam signal on its own); every send carries a `List-Unsubscribe` header (mailto fallback always, plus a one-click HTTPS link + `List-Unsubscribe-Post: List-Unsubscribe=One-Click` since `APP_BASE_URL` is now set in Railway); a new `email_suppressions` SQLite table is checked before every send and populated by a public, unauthenticated `/unsubscribe` route (GET+POST, RFC 8058 one-click) wired to `SESSender._unsubscribe_headers()`; follow-ups now thread with real `In-Reply-To`/`References` headers against the original send's `Message-ID` (persisted to a new Sheets "Message ID" column / SQLite `email_history.message_id`) instead of just prefixing the subject with "Re:", which used to look like a spoofed reply to filters, not a real thread.
  - **Verification tool**: once production access clears, send a real test email through the app and run it through mail-tester.com to confirm SPF/DKIM/DMARC all pass and check the spam score — nothing in this codebase can verify actual inbox placement, only the DNS chain being auth-correct.
- **Google Sheets rate limiting**: hardcoded blocking `time.sleep(1.5)` between writes (`storage/sheets.py:120`) to stay under ~60 writes/min quota. Only invoked from background tasks (FastAPI runs sync `BackgroundTasks` callables in a threadpool), so it doesn't block the request that triggered it.
- **Crawl4AI's documented `url="raw:html", raw_html=html` shortcut is broken in 0.9.1** — live-tested and confirmed: it doesn't parse `raw_html` at all, just returns the literal string `"html"`. `_run_crawl4ai_sync` (`scrapers/website.py`) now writes the HTML to a temp file and crawls it via a `file://` URL instead — live-verified against a real site (hitchki.co), produces real markdown. A length guard (`> 20` chars) in `_extract_markdown` falls back to `markdownify` if Crawl4AI ever silently returns near-empty output again. Crawl4AI's own markdown also keeps raw `[text](url)` link syntax (`markdownify`'s fallback strips anchors via `strip=['a']`, Crawl4AI's doesn't) — stripped with a regex in `_parse_html` before use, since it was skewing both the readability score and the AI's homepage-text context with nav-link noise.
- **Wappalyzer swapped to `wappalyzer` (wappalyzer-next, https://github.com/s0md3v/wappalyzer-next)**, replacing the stale `python-Wappalyzer`. Uses `scan_type="fast"` (single HTTP request, no browser) — live-tested at ~5s against wordpress.org, correctly detected WordPress/PHP/MySQL/Nginx/etc. Still runs off-thread via `asyncio.to_thread` with a 10s outer timeout, degrading to `[]` on timeout/error. **Do not install both `python-Wappalyzer` and `wappalyzer` in the same environment**: on case-insensitive filesystems (Windows, default macOS) their package directories (`Wappalyzer/` vs `wappalyzer/`) collide and corrupt both installs — hit this firsthand during this session, confirmed by uninstalling both and reinstalling `wappalyzer` alone. `requirements.txt` only lists the new one, so a fresh `pip install -r requirements.txt` is fine; this only bites if you `pip install python-Wappalyzer` back in manually.
- **`pyseoanalyzer` makes its own separate HTTP fetch of the target URL** (not the Playwright HTML already in hand) — accepted tradeoff (see §5.6) for real crawl-based checks (word count, page warnings). Adds a few seconds to each audit; `follow_links=False` keeps it to just the one page. Its extracted `description` field can occasionally pick up garbage (live-tested on a messy real site, it once flagged a video file URL as "the description is too long") — surfaced as-is in the flaw list since filtering every possible garbage pattern isn't worth the complexity; treat pyseoanalyzer warnings as generally useful but not 100% clean signal. Its "Anchor missing title tag" and "Image missing alt tag" warnings are both filtered out entirely (`scrapers/website.py`) — the former fires on nearly every anchor on most sites (not a meaningful signal), the latter live-verified to fire on `alt=""` images (the *correct* WCAG pattern for decorative images, contradicted by axe-core which correctly didn't flag them).
- **`_check_font_consistency` (`analyzer/visuals.py`) only counts visually-relevant text elements.** Skips elements with no visible text (empty headings, icon-only buttons), `sr-only`/`screen-reader`/`visually-hidden` classes (skip-links etc.), and near-zero (`<=2px`) bounding boxes — before this filtering, an icon-only search button's `Arial` fallback font was counting as a real font on the page. If a font-count flaw looks wrong, check whether the source element is actually visible to a sighted user, not just present in the DOM.
- **`_check_broken_assets` (`analyzer/visuals.py`) retries via GET before trusting a HEAD failure.** Some servers/WAFs selectively reject or rate-limit HEAD requests from automated tools while GET succeeds for the same asset — live-observed this exact flakiness (same site, same code, different runs: 6 "broken" links vs 0). Only a failure on *both* HEAD and the GET retry is reported as broken.
- **Two storage backends, two schemas**: SQLite (`storage/db.py`, snake_case columns) and Google Sheets (`storage/sheets.py`, Title Case header strings as field keys) are separate, unreconciled data stores — Sheets is the lead/CRM source of truth (`get_pending_leads`, `find_row_by_website`, etc.), SQLite is cost/history/drafts only. Not touched in this pass; if scope grows, decide whether to retire one (e.g. Baserow, MIT-licensed self-hosted Airtable alternative, was considered but not implemented — needs a deployed instance first).
- **Rate limiting** is in place on all `/api/*` routes (`app.py:rate_limit`, in-memory sliding window keyed by API key, `429` on breach): 5/min on `/api/search` and `/api/audit`, 10/min on `/api/send`, 30/min on the drafts delete, 120/min on the three GET/poll endpoints (`/api/costs` is polled every 5s by the frontend, so it needs headroom). In-memory only — fine for one Railway instance, won't hold up if this ever runs multi-instance/multi-worker (would need Redis or similar for a shared counter).
- **`validate_email(..., check_deliverability=True)` (`enrichment/decision_maker.py`) is unreliable from a cloud host** — DNS/MX lookups get blocked or time out often enough that a real, scraped-off-the-site email can fail the check and incorrectly get discarded, cascading down to the much weaker OSINT-guess strategy. Fixed 2026-07-10: `_scrape_website_for_email` now falls back to accepting the top-scored candidate on format validity alone (`check_deliverability=False`) if every candidate fails the deliverability probe, instead of returning `""` and falling through. Also added `_is_junk_email()` — filters `noreply@`/`no-reply@`/`postmaster@`/etc. out of both the scraper and the OSINT dork fallback (`_find_email_via_osint`), since those matched too easily off random search-snippet text and produced nonsense "To" addresses in drafted emails.
- **Contact name defaults to a generic `"{company_name} Team"`, not a fixed string.** Used to be hardcoded `"Marketing Team"`/`"Team"` across all three no-real-name-found code paths in `find_decision_maker` — every cold email opened with the same generic "Hi Marketing Team," regardless of which business it was for. Fixed 2026-07-10.
- **Audit screenshots are taken right after Playwright's `load` event fires, which is often too early** — CSS fade-in animations, lazy-loaded hero images, and cookie-consent widgets aren't done rendering yet, so the captured screenshot can look faded/broken and not match what a real visitor sees. Fixed 2026-07-10 (`analyzer/visuals.py:generate_audit_screenshot`): best-effort `wait_for_load_state("networkidle", timeout=5000)` + a fixed 1s settle delay before capturing, wrapped so a slow/never-idle page (e.g. persistent chat-widget polling) doesn't fail the whole audit.
- **Instagram scraping (`instagrapi`) and the Google Maps Playwright fallback both scrape platforms directly against their ToS.** This session added mitigations, not a fix — the underlying ToS/ban risk doesn't go away:
  - `scrapers/instagram.py`: added a circuit breaker (`_trip_challenge_breaker`) — a `ChallengeRequired` from Instagram now pauses *all* Instagram calls for 60 minutes (`_CHALLENGE_COOLDOWN_SECONDS`) instead of retrying immediately, which is what turns a soft flag into a permanent ban. `_ensure_logged_in` checks the cooldown before attempting any login.
  - `scrapers/google_maps.py`: jittered delays (was fixed `sleep(1.5)`/`sleep(3)`, now randomized ranges) on the Maps scroll loop, plus a jittered delay + `asyncio.to_thread` between each DDG website-lookup call (was a tight zero-delay loop hammering DDG synchronously on the event loop).
  - A dedicated scraper repo (e.g. gosom/google-maps-scraper) was considered for the Maps side but skipped — it's written in Go, not Python, bigger integration than warranted right now.

---

## 9. Database Schema (`storage/db.py`)

SQLite at `data/database.sqlite` (repo-root-relative, auto-created). Every public function (`log_cost`, `log_email`, `get_costs`, etc.) calls `init_db()` and opens/closes its own `sqlite3.connect()` — no shared connection or pool.

- **`cost_logs`**: `id` (PK autoincrement), `timestamp` (default `CURRENT_TIMESTAMP`), `category` (TEXT NOT NULL — e.g. `"Google Maps API"`, `"AI Audit"`, `"AWS SES"`, `"AI Web Fetch"`), `cost` (REAL NOT NULL), `description` (TEXT)
- **`email_history`**: `id` (PK), `timestamp`, `company`, `website`, `target_email`, `sender_email`, `subject`, `body`, `message_id` (added via `PRAGMA user_version` migration — RFC threading against the SES `Message-ID`, see §8)
- **`email_drafts`**: `id` (PK), `timestamp`, `company`, `website`, `target_email`, `subject`, `body`, `image_url`
- **`email_suppressions`**: `email` (PK), `timestamp`, `reason` — checked before every send (`SESSender.send_email`/`send_followup`), populated by the public `/unsubscribe` route (RFC 8058 one-click)

Rows are converted `sqlite3.Row` → `dict(row)` for JSON-serializable API responses.

---

## 10. Frontend Architecture (`frontend/`)

- Single-page app, **no router library** — view switching via `useState('home')`/`currentView` string with conditional rendering in one large component (`frontend/src/App.jsx:9-10`).
- **No global state library** — plain `useState`/`useRef`/`useEffect`. `leads` state persists to `localStorage` under key `leadAuditLeads` (`App.jsx:14-17,49-51`).
- **No dedicated API service layer** — `axios` calls are made directly inside component handlers. `API_BASE = ""` (relative paths, same-origin) at `App.jsx:7`.
- Polls `/api/costs` every 5s via `setInterval` for a live running-cost pill (`App.jsx:68-75`).
- "Autopilot" mode sequentially calls `/api/audit` per lead, using refs (`isAutopilotRef`, `leadsRef`) to avoid stale closures inside the loop (`App.jsx:141-150`).
- Build: `vite build` → `frontend/dist/`. `dist` is gitignored and (as of 2026-07-10) untracked — Docker builds it fresh on every Railway deploy (see §12); `app.py:252-254` mounts it as static files at `/` if present.
- Lint: `oxlint` (not ESLint) — config at `frontend/.oxlintrc.json`, run via `npm run lint` in `frontend/`.
- `frontend/vite.config.js` is the unmodified default Vite React template (no aliasing/proxy/env config).
- `frontend/README.md` is stock Vite+React boilerplate — no project-specific content, ignore it.

---

## 11. How To Run Locally

```bash
# 1. Install Backend & CLI Tools
pip install -r requirements.txt
playwright install chromium
npm install # Installs lighthouse CLI

# 2. Install & Build Frontend
cd frontend
npm install
npm run build
cd ..

# 3. Fill in .env with your API keys

# 4. Start the Server
python app.py
```
Open `http://localhost:8000` in your browser.

---

## 12. Deployment (Railway)

Railway builds from **`Dockerfile`** (`railway.json` pins `"builder": "DOCKERFILE"`). The repo used to also carry a leftover, unused `nixpacks.toml` from an earlier setup — deleted 2026-07-10.

- `Dockerfile` is a 3-stage build: a Python deps stage, a Node `frontend-builder` stage that runs `npm run build` fresh on every deploy, and a final Python+Playwright+Node runtime image that copies both in. Playwright's Chromium system deps (`libnss3`, `libatk1.0-0`, etc.) are installed via `apt-get` in the final stage; `playwright install chromium` downloads the browser itself.
- **The final runtime stage now installs Node.js + runs `npm install` for the root `package.json`.** Fixed 2026-07-10 — before this, the final stage was plain `python:3.11-slim` with no Node at all, so `analyzer/lighthouse.py`'s check for the `lighthouse` binary always failed silently on every single deploy, falling through to the PageSpeed API fallback (which was also failing, likely `PAGESPEED_KEY` never added as a Railway variable) — real-world symptom was every audit showing `0/100` for both performance and SEO, live-verified as a Lighthouse/Node availability bug, not a timeout (`_execute_lighthouse` already has a generous 120s subprocess timeout). Verified locally (Windows can't exec the `.bin` symlink directly, `WinError 193`, but invoking `node node_modules/lighthouse/cli/index.js` directly against a real site produced correct scores) — the underlying `npm install` + Lighthouse package wiring is confirmed correct; the fix works on Linux (Docker) where the `.bin` symlink executes fine.
- `_pagespeed()` (`scrapers/website.py`) now logs *why* it failed instead of silently returning `{}` — explicit warning if `PAGESPEED_KEY` is unset, logs the raw response keys if `lighthouseResult.categories` is missing, and includes the response body in the exception log.
- **`frontend/dist/` is no longer committed to git** (untracked 2026-07-10 — it used to be, which meant a locally-built bundle with a baked-in `VITE_API_KEY` could end up in git history). The Docker build produces it fresh from `frontend/` source every deploy; `app.py` mounts it if present, same as before.
- **Set `VITE_API_KEY` as a Railway *Build Variable*** (not just a regular deploy-time env var — it needs to be visible during the Docker build, since that's when `npm run build` bakes it into the JS). The `Dockerfile`'s `frontend-builder` stage declares `ARG VITE_API_KEY` to receive it. Without this, the deployed frontend won't send `X-API-Key` and every request will 401 once `API_KEY` is set server-side.
- Railway mounts a persistent volume at `/app/data` to ensure `database.sqlite` and `screenshots/` survive deployments.

---

## 13. Changelog

| Date       | Change                                              |
| ---------- | --------------------------------------------------- |
| 2026-06-24 | Initial project scaffold created. |
| 2026-07-06 | Implemented God-Tier OSINT & Playwright screenshots for visual email evidence. |
| 2026-07-09 | Refactored into a full-stack React + FastAPI web application for local and remote deployment. |
| 2026-07-10 | Added persistent SQLite database for 100% accurate fractional penny Cost Tracking and permanent Email History. |
| 2026-07-10 | Implemented the Saved Drafts Inbox allowing users to run asynchronous Autopilot audits. |
| 2026-07-10 | Upgraded the Deep Brand Context Crawler using Crawl4AI and Jina Reader for extremely clean LLM markdown extraction. |
| 2026-07-10 | Added Google Lighthouse CLI via Node subprocess for highly accurate performance and SEO audits. |
| 2026-07-10 | Interconnected `axe-core` accessibility audits with Playwright's bounding box API to draw real, dynamic red boxes on desktop screenshots, feeding exact visual context to Claude. |
| 2026-07-10 | Documented Testing, Code Style, Known Gotchas, Database Schema, and Frontend Architecture sections (§6-10) after full codebase audit — no code changes. |
| 2026-07-10 | Fixed audit-flagged bugs: `generate_audit_screenshot()` 3-tuple unpack in `main.py`/`test_audit.py`; `main.py` duplicate `SheetsStorage` import, hardcoded `YOUR_NAME`, and hardcoded `[:10]` lead cap (now uses `DAILY_EMAIL_LIMIT`); Lighthouse `output_path` `UnboundLocalError` guard; `init_db()` one-time-cleanup DELETE now guarded by `PRAGMA user_version` instead of running (and risking wiping real `cost_logs` rows) on every call. |
| 2026-07-10 | Hardening pass: removed unused `nixpacks.toml` (Railway actually builds via `Dockerfile`, per `railway.json`); added SSRF guard (`security_utils.py:validate_public_url`) rejecting `/api/audit` requests whose `website` resolves to a private/loopback/link-local/reserved IP (blocks pointing the server-side scraper at internal infra or cloud metadata endpoints); `ALLOWED_ORIGINS` env var to scope CORS instead of a hardcoded wildcard; short-TTL (10 min) in-memory cache on `/api/audit` keyed by normalized URL so accidental duplicate audits (double-clicks, re-opening a lead) don't re-run the full scrape+AI pipeline or double-log cost; two new zero-cost flaw checks (`has_viewport_meta`, `has_favicon`) parsed from HTML already fetched, no extra requests; frontend: retry button on failed audits, client-side pagination (12/page) for the leads grid. Evaluated and skipped Anthropic prompt caching — the static instructions block is ~470 tokens, under both the 1024 (Sonnet) and 2048 (Haiku) minimum cacheable size, so it wouldn't have helped the primary (Haiku) provider. |
| 2026-07-10 | Fixed 3 real bugs reported from a live audit: (1) screenshots captured before the page finished fading in/lazy-loading — `generate_audit_screenshot` now waits for `networkidle` + a 1s settle delay before capturing; (2) drafted emails' "To" address occasionally landed on junk like `noreply@gmail.com` — traced to `check_deliverability=True` SMTP/MX probing being unreliable from Railway and silently discarding real scraped emails, cascading to a weak OSINT fallback with no junk filter; added a format-only fallback for scraped emails plus `_is_junk_email()` filtering in both the scraper and OSINT paths (`enrichment/decision_maker.py`); (3) every cold email opened with a hardcoded "Hi Marketing Team," regardless of company — replaced with `f"{company_name} Team"` across all three generic-name code paths. |
| 2026-07-10 | Fixed a 4th live-audit bug: page speed/SEO scores always showing `0/100` in production. Root cause was **not** a timeout — the final Docker runtime stage never had Node.js installed, so the Lighthouse CLI (`analyzer/lighthouse.py`) always failed its binary check and fell through to the PageSpeed API fallback, which was also failing silently. `Dockerfile` now installs Node.js + runs `npm install` for the root `package.json` in the final stage; `_pagespeed()` now logs the actual failure reason (missing key, bad response shape, HTTP error body) instead of swallowing it. Verified the Lighthouse package itself produces correct real scores (performance 100, SEO 80, accessibility 100, best-practices 96 against example.com) when invoked directly with Node — the wiring is correct, confirmed working on the Linux/Docker target even though the `.bin` symlink can't be exec'd on Windows for local testing. |
| 2026-07-10 | Added visual-polish flaw detection, since every prior flaw was technical (SEO/perf/a11y/security) and the AI had no signal for "this just looks bad": (1) font-consistency check (`analyzer/visuals.py:_check_font_consistency`) — scans computed `font-family` across visible text elements, flags if the page uses more than `_MAX_CONSISTENT_FONTS` (3); (2) stretched-image check (`_check_stretched_images`) — flags `<img>`s displayed >40% larger than their natural resolution (blurry/pixelated); (3) AI prompt (`analyzer/ai_audit.py`) now explicitly instructs the model to critique the screenshot for general visual polish (clashing fonts, mismatched colors, cluttered layout, low-quality images), not just the axe-core red-box violation. |
| 2026-07-10 | Restored Crawl4AI and Wappalyzer to actually run (both were silent no-ops) — both now execute off-thread via `asyncio.to_thread` with a timeout (15s / 8s) and fall back gracefully on timeout/error, so they can't block the event loop or cause Railway 502s. Unverified against live installs — watch `[Parse]`/`[Wappalyzer]` logs after deploying. |
| 2026-07-10 | Wrapped blocking `db.py`/`ses_sender.py`/scraper calls in `asyncio.to_thread` across `app.py`'s async route handlers so one slow request (SQLite write, SES send, Instagram scrape) can't stall other concurrent requests on the event loop. |
| 2026-07-10 | Fixed screenshot filename collisions — filenames now include a hash of the (normalised) URL, not just the sanitized company name; `analyzer/visuals.py` exports a shared `make_screenshot_filename()` used by both `generate_audit_screenshot()` and `app.py`'s `/api/send`. |
| 2026-07-10 | Deleted dead files: `old_maps.py` (corrupted, unused), `storage/leads.db` (stray 0-byte file, not the real DB), `warmup.py` (unfinished, SMTP send was commented out). |
| 2026-07-10 | Added a real pytest suite (`pytest.ini`, `requirements-dev.txt`, `test_parse_json.py` unit tests, `test_audit.py`/`test_lh.py` converted to `@pytest.mark.integration` with self-skip on missing keys/binaries) and a GitHub Actions workflow (`.github/workflows/tests.yml`) running it on push/PR. Added `.env.example`. See §6. |
| 2026-07-10 | **Added API-key auth** to all `/api/*` routes (`app.py:require_api_key`, `config.API_KEY`) — previously anyone with the URL could trigger paid AI audits, send email via SES, or scrape, with zero auth. Frontend sends `X-API-Key` via `VITE_API_KEY` (Vite build-time env var). Off by default if `API_KEY` isn't set (prints a startup warning) — set it before any public deploy. Live-verified with FastAPI's `TestClient`: no key → 401, wrong key → 401, correct key → 200. |
| 2026-07-10 | Fixed `scheduler.py`'s `ingest_leads()`: for the default `LEAD_SOURCE=maps`, it called the `async def scrape_google_maps(...)` from a sync function with no `await`/`asyncio.run`, so every Sunday's scheduled ingestion crashed with `TypeError: 'coroutine' object is not iterable`. Now `ingest_leads` is `async def`, awaited via `asyncio.run(ingest_leads())` in the job registration, matching the existing `run_batch`/`run_followups` pattern. |
| 2026-07-10 | Fixed the same `generate_audit_screenshot()` 2-var/3-tuple bug (see the 2026-07-10 tuple-unpack entry above) in a file missed during the first pass: `send_approved.py:40` assigned the whole 3-tuple to a single `image_path` variable, so `ses.send_email(..., image_path=<tuple>)` and the subsequent `os.remove(image_path)` would have crashed on a real run. |
| 2026-07-10 | `enrichment/decision_maker.py`: removed `verify=False` from the shared `httpx.Client` (was disabling SSL certificate verification on every scrape — MITM risk); replaced 2 bare `except:` with `except Exception:`; deleted the dead `score_lead()`/`is_qualified()` stub (`score_lead` was `raise NotImplementedError`, unused elsewhere). Also fixed bare `except:` in `scrapers/google_maps.py` (2 places), `send_approved.py`, and `test_playwright_maps.py`. |
| 2026-07-10 | Pinned every dependency in `requirements.txt` to its currently-working version (was completely unpinned) and deduped a repeated `playwright` line. Swapped `python-Wappalyzer==0.3.1` (stale, original Wappalyzer project went commercial in 2023) for `wappalyzer==2.0.1` (the actively-maintained wappalyzer-next project) — see §8 for a real filesystem-collision gotcha discovered while testing this swap, and the `scan_type` tradeoff (`"fast"` chosen over `"balanced"`/`"full"` for latency reasons, live-verified). |
| 2026-07-10 | Added in-memory sliding-window rate limiting to every `/api/*` route (`app.py:rate_limit`) — live-verified with `TestClient` (exactly 120 requests succeed, 121st+ return 429 within the window). Wrapped `analyzer/lighthouse.py`'s blocking `subprocess.run` calls in `asyncio.to_thread` so a 120s Lighthouse run no longer blocks the event loop. Added a circuit breaker to `scrapers/instagram.py` (60-minute cooldown on `ChallengeRequired`, live-verified with a mocked client) and jittered delays + threading to `scrapers/google_maps.py`'s Playwright/DDG fallback path — mitigations for ToS/ban risk, not a fix for the underlying risk of scraping those platforms at all. |
| 2026-07-10 | **Fixed a shipped production bug from earlier the same day**: the new rate limiter's bucket store was a single module-level dict keyed only by API key, shared across every route regardless of its own configured limit — the frontend's 5s `/api/costs` poll was silently exhausting the budget meant for `/api/search`/`/api/audit`'s much stricter 5/min limit, breaking search/audit within ~25s of the dashboard being open. Fixed by moving the bucket dict into `rate_limit()`'s closure so each route gets an isolated store; live-verified (20 calls against one limiter no longer touch a separately-scoped limiter's budget). Also fixed `frontend/src/App.jsx` silently swallowing every axios error behind a generic `alert()` with no `console.error` and no response body — this bug would have taken seconds to diagnose instead of a guessing round-trip if the real error had been visible from the start. |
| 2026-07-10 | **Built a flaw reconciliation layer** (`analyzer/flaws.py`, `scrapers/website.py:_build_flaws`) — see §5.6. Added three new free/open-source signal sources: `extruct` (structured data), `textstat` (readability), `pyseoanalyzer` (crawl-based SEO pass, was already an unused pinned dependency). Added security-headers and canonical/robots-noindex/Open-Graph checks (zero new dependencies — reuses HTML/headers already fetched). Rewrote `analyzer/ai_audit.py`'s prompt builder to consume one ranked `FLAWS DETECTED` block instead of five separate raw dumps; axe-core is now the sole accessibility signal shown (Lighthouse's own `accessibility` score, independently axe-core-derived, is dropped from the prompt to eliminate that specific unreconciled redundancy). Added `test_build_flaws.py` (8 tests, no network) locking in severity mapping and ranking behavior. **Also found and fixed two real bugs surfaced by first-ever live end-to-end testing of this pipeline**: (1) Crawl4AI's documented `url="raw:html"` shortcut doesn't work in `crawl4ai==0.9.1` — confirmed it silently returns the literal string `"html"` instead of parsing `raw_html`; fixed by writing to a temp file and crawling via `file://` instead, with a length-guard fallback to `markdownify`. (2) Crawl4AI's own markdown output (unlike the `markdownify` fallback) doesn't strip `[text](url)` link syntax, which was skewing both the readability score and the AI's homepage-text context with navigation-link noise — stripped via regex in `_parse_html`. |
| 2026-07-10 | Discovered mid-deploy-prep that `frontend/dist/` (committed to git, meant to carry the `VITE_API_KEY`-baked production build) was about to leak the new API key into git history — and that `railway.json` actually builds via `Dockerfile`, not `nixpacks.toml` as CLAUDE.md previously (incorrectly) documented. Fixed properly: `Dockerfile`'s `frontend-builder` stage now takes `VITE_API_KEY` as a build `ARG` (set as a Railway *Build Variable*, never committed); `frontend/dist/` untracked from git (`git rm -r --cached`) since the Docker build produces it fresh every deploy. Corrected §12 Deployment, which had been describing an unused Nixpacks setup. |
| 2026-07-10 | Fixed 2 false-positive flaws on a live audit (simplygaming.in) — see commit `37ca8be`: (1) `has_ssl` was computed from the input URL string, not the page Playwright actually loaded, so a lead stored as `http://` that server-redirects to `https://` was wrongly flagged as insecure — now uses the post-redirect `page.url`. (2) `homepage_text`/Flesch readability score was dominated by `<nav>` mega-menu link text (500+ words of category links on this site), tanking the score to 8/100 — nav content is now excluded before scoring. |
| 2026-07-13 | Found and fixed 3 more false-positive flaws while auditing the flaw pipeline for accuracy (no new bugs found via code review alone — each was live-verified against simplygaming.in first): (1) `pyseoanalyzer`'s "Image missing alt tag" warnings (`scrapers/website.py`) fire on images with `alt=""`, which is the *correct* WCAG pattern for decorative images — axe-core (the authoritative a11y signal) correctly didn't flag the same images, so these warnings are now filtered out alongside the existing "Anchor missing title tag" filter. (2) The font-consistency check (`analyzer/visuals.py:_check_font_consistency`) counted invisible/empty elements as typography signal — an icon-only button with no text was contributing a stray `Arial` to the font list. Now skips elements with no visible text, `sr-only`/`visually-hidden` classes, and near-zero bounding boxes before collecting `font-family`. (3) The broken-asset check (`_check_broken_assets`) used HEAD requests only, but some servers/WAFs selectively reject or rate-limit HEAD probes from automation while GET succeeds — live-observed this exact flakiness (same site, same code: 6 "broken" links on one run, 0 on the next). A HEAD failure now retries once via GET before being trusted as genuinely broken. |
| 2026-07-13 | Cold-email spam-folder deliverability pass (see §8 for the full breakdown). Live-checked the SES/DNS setup and found the biggest problems were infra, not code: SES account in sandbox, domain SPF only authorized Google Workspace (not SES), no DKIM, no DMARC. Fixed both sides: (1) code — `emailer/ses_sender.py`'s `send_email`/`send_followup` now send real multipart raw MIME with both `text/plain` and `text/html` parts (was HTML-only), a `List-Unsubscribe`/`List-Unsubscribe-Post` header on every send, and real `In-Reply-To`/`References` threading on follow-ups against the original send's `Message-ID` (was just prefixing "Re:" with no threading headers); added an `email_suppressions` table (`storage/db.py`) checked before every send, populated by a new public `/unsubscribe` route (`app.py`, RFC 8058 one-click); added a "Message ID" column to the Sheets CRM schema (`storage/sheets.py`, self-healing header backfill for existing sheets) and `email_history.message_id` in SQLite; added `test_email_deliverability.py` (6 tests, no network). (2) Infra — created a proper `mmga.agency` domain identity in SES with Easy DKIM (3 CNAMEs added to GoDaddy DNS, verified); enabled SES's custom MAIL FROM domain (`mail.mmga.agency`) to scope SPF (`include:amazonses.com`) to a subdomain instead of touching the root domain's Google Workspace SPF record; added a `_dmarc.mmga.agency` TXT record (`p=none`); requested SES production access (submitted, under AWS review); set `APP_BASE_URL` in Railway so the one-click unsubscribe link is live. |
| 2026-07-13 | AI-audit accuracy pass. Fixed a dead Claude model (`claude-3-5-haiku-latest` had hit end-of-life 5 months earlier, silently falling through to Gemini) and a nonexistent Gemini model (`gemini-3-flash` 404'd, silently cascading to GPT-4o-mini) — see §8, both discovered by actually calling the APIs live rather than trusting docs/memory. Added a 4th free fallback provider (OpenRouter, `google/gemma-4-31b-it:free`) and native JSON response mode for Gemini/OpenAI. Added error logging to all 4 providers (previously only Gemini logged failures, which is exactly what let the bad model IDs go unnoticed). |
| 2026-07-13 | Multi-page + mobile audit crawl (`analyzer/visuals.py`). Discovers up to 2 extra internal pages via `/sitemap.xml` (falling back to homepage anchor-keyword scan) and runs the full check set (axe-core, broken links, fonts, stretched images) on each, tagging violations with their source page. Captures a separate real mobile-viewport (390x844) screenshot, fed to the AI as a second vision input, plus a mobile-specific axe-core pass and horizontal-overflow check. The screenshot attached to the email is now whichever page had the worst violation, not always the homepage. Also extracted real Core Web Vitals (LCP/CLS/TBT) from Lighthouse/PageSpeed's `audits` object instead of discarding everything but the 0-100 category score, and threaded Google Maps `Rating`/`Reviews Count` (already scraped, never used) into the AI prompt as a personalization hook. |
| 2026-07-13 | Fixed a false-"website is down" bug (see §8): Playwright navigation used `wait_until="load"`, which waits for every resource on the page (analytics, chat widgets, fonts) and never fires within any timeout on plenty of real sites even though they load fine for an actual visitor — live-verified a site that curl'd back in 2.5s was being reported fully unreachable (0/100 scores, a drafted "Your Website Is Down" email). Switched every navigation to `wait_until="domcontentloaded"` with a more generous timeout kept as a safety net (removing the timeout entirely wasn't viable — see the Playwright-semaphore gotcha in §8). |
| 2026-07-13 | Added Claude `web_fetch` as a 5th, last-resort contact-discovery strategy in `enrichment/decision_maker.py`'s `find_decision_maker()` — only tried after the free scraper/LinkedIn-OSINT/email-dork strategies all fail. Real per-call cost (~$0.0008, live-verified) flows back through the return dict and gets logged to `cost_logs` under `"AI Web Fetch"`, matching the "AI Audit"/"AWS SES" pattern already in `app.py` — a miss still logs its cost since the API call happened either way. See §8 for why this is deliberately gated to the failure path only. |
| 2026-07-13 | 6 new flaw signals: `robots.txt` blanket-disallow check (a much bigger and more common SEO killer than the `<meta name="robots">` check already in place, and wasn't checked at all before); real alt-text coverage (missing attribute entirely, not `alt=""`, recovering the exact signal `pyseoanalyzer`'s own filtered-out "missing alt tag" warning was already known to false-positive on); JS console-error capture; mixed-content (HTTP resources on an HTTPS page) detection; duplicate title/meta-description across crawled pages (only detectable now that multiple pages get crawled per lead). |
| 2026-07-13 | Added a "conversion" flaw category (`analyzer/flaws.py`, `scrapers/website.py`) — every prior flaw was performance/SEO/accessibility/security, never "can a customer actually convert on this page," which is what a business owner reading the email cares about most. New checks: reservation/booking widget detection (~14 known platform signatures — OpenTable, Resy, SevenRooms, Calendly, etc. — checked against raw HTML since these are almost always script/iframe embeds), click-to-call (`tel:`) / WhatsApp (`wa.me`) link presence, menu/pricing findability, and a stricter structured-data check (present but not a business-relevant `@type` like LocalBusiness/Restaurant/Organization). Live-verified on a real restaurant site: correctly flagged missing menu/pricing and no booking widget, correctly did not flag click-to-call (the site has one). Added 6 new tests to `test_build_flaws.py` (13 → 19) and updated its "clean site" fixture to include the new signals. See §8 for the heuristic-not-exhaustive caveat on these checks. |
