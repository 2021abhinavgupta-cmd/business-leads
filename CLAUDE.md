# CLAUDE.md — Lead Audit Bot

> **MANDATORY**: Read this file in full before writing or modifying any code in this project.

---

## 1. Project Overview

**lead-audit-bot** is an automated B2B lead-generation and AI audit pipeline that operates as a modern Full-Stack Web App. It features:

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
├── scrapers/
│   ├── __init__.py
│   ├── google_maps.py         # GoogleMapsScraper — SerpAPI / Places API
│   ├── instagram.py           # InstagramScraper — instagrapi
│   └── website.py             # WebsiteScraper — Playwright + Crawl4AI + Jina Reader
├── enrichment/
│   ├── __init__.py
│   └── decision_maker.py      # DuckDuckGo OSINT & Email Validation
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
├── nixpacks.toml              # Railway multi-language (Python + Node) build configuration
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
| **AI Models**   | `google-generativeai`, `openai`, `anthropic` |
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
| `ANTHROPIC_API_KEY`  | Anthropic Claude API          |
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
- Aggregates Lighthouse scores, PageSpeed fallbacks, security headers (`extruct`), structured data (`extruct`), readability (`textstat`), a crawl-based SEO pass (`pyseoanalyzer`), axe-core violations, and broken link data — all reconciled into one ranked flaw list by `_build_flaws()` (see §5.6).

### 5.3 `analyzer/lighthouse.py`
- Runs the Google Lighthouse CLI via a Node.js subprocess to gather `performance`, `seo`, `accessibility`, and `best_practices` scores natively. `accessibility` is fetched but not surfaced to the AI prompt — axe-core is the authoritative a11y signal (see §5.6).

### 5.4 `analyzer/visuals.py`
- Headless Playwright script running a standard Desktop (1280x800) viewport.
- **Interconnected Red Box Pipeline**:
  1. Runs `axe-core` accessibility audit.
  2. Extracts the exact bounding box `(x, y, w, h)` of the most severe UI violation.
  3. Draws a precise red box around the real flaw using `Pillow`.
  4. Passes the `visual_flaw_context` up the chain to the AI prompt.
- Also captures the Playwright navigation's HTTP response headers (`response.headers`, free — reuses the request already being made for the screenshot) for the security-headers check in `website.py`.

### 5.5 `analyzer/ai_audit.py`
- Ingests `company_context` to force extreme outreach personalization.
- Takes in the `visual_flaw_context` and is strictly instructed to explicitly mention the red box drawn on the screenshot in the email body.
- Prompt consumes one pre-ranked `FLAWS DETECTED` section (from `web.flaws`) instead of five separate raw tool-output dumps — see §5.6.

### 5.6 `analyzer/flaws.py` — the flaw reconciliation layer
Every audit tool used to dump its raw output into the AI prompt as its own unranked text block, leaving the LLM to figure out what mattered and silently absorb redundant/contradictory signals — most notably, Lighthouse's own `accessibility` score and a separate direct axe-core scan both measured accessibility independently, never compared.

As of 2026-07-10, all tool output gets normalized into a common `Flaw(category, severity, description)` type in `scrapers/website.py:_build_flaws()`, deduplicated/reconciled in code (axe-core wins over Lighthouse's `accessibility` number; the two are never both shown), ranked by severity via `analyzer/flaws.py:rank()`, and handed to the AI as one `FLAWS DETECTED (ranked most severe first)` block. The AI's job shifted from "find the flaws in this raw dump" to "write compelling copy about the 2-3 most severe items on this pre-ranked list" — more consistent results, and the reconciliation logic is visible/testable in code (`test_build_flaws.py`) rather than being an LLM judgment call each time.

Signals feeding into it: Lighthouse/PageSpeed scores, SSL, meta title/description/H1, CTA/contact/testimonials/blog presence, canonical tag, robots noindex, Open Graph tags, structured data (`extruct`), readability (`textstat`), thin content + real SEO warnings (`pyseoanalyzer`), security headers, axe-core violations, broken links.

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
| `test_build_flaws.py` | Unit, no network | 8 tests locking in `_build_flaws()`'s severity mapping and ranking behavior (see §5.6). Always runs, always fast. |
| `test_audit.py` | `@pytest.mark.integration` | Full pipeline (Playwright → audit → AI) against a real Timezone Games URL. Self-skips via `skipif` when no `ANTHROPIC_API_KEY`/`GEMINI_API_KEY`/`OPENAI_API_KEY` is set. |
| `test_lh.py` | `@pytest.mark.integration` | Runs real Lighthouse CLI against a real URL. Calls `pytest.skip()` at runtime if no Lighthouse binary is reachable. |

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
- **Playwright concurrency capped at 1** via global `asyncio.Semaphore(1)` (`analyzer/visuals.py:15`) to avoid OOM on Railway's 500MB instances — audits run serialized, not parallel. This is a deliberate resource constraint, not a bug; revisit only if upgrading the Railway plan or moving audits to a separate worker.
- **AI provider fallback chain** (`analyzer/ai_audit.py:86-101`): Claude Haiku (`claude-3-5-haiku-latest`) → Gemini (`gemini-2.0-flash`) → GPT-4o-mini, each only constructed if its API key is set. Cost is estimated locally with hardcoded per-token pricing constants, not fetched from any API. JSON parsing of AI output (`analyzer/ai_audit.py:_parse_json`) strips markdown fences, slices between first `{`/last `}`, and returns `None` on parse failure (no retry — falls through to the next provider). If every provider fails, `analyze_lead` now logs `[AIAuditor] All AI providers failed...` before returning `None` (previously silent).
- **SES retry/rate-limiting** (`emailer/ses_sender.py:145-165`): `"Daily message quota exceeded"`/`LimitExceeded` → hard raise; `MessageRejected` → soft `False`; `Throttling` → one retry after blocking `time.sleep(5)`. Still runs on the event loop when called directly from an async route, but those call sites (`app.py`) now wrap it in `asyncio.to_thread`, so it no longer blocks other concurrent requests — the `time.sleep(5)` itself is unchanged (still blocks that one worker thread for up to 5s on throttle).
- **Google Sheets rate limiting**: hardcoded blocking `time.sleep(1.5)` between writes (`storage/sheets.py:120`) to stay under ~60 writes/min quota. Only invoked from background tasks (FastAPI runs sync `BackgroundTasks` callables in a threadpool), so it doesn't block the request that triggered it.
- **Crawl4AI's documented `url="raw:html", raw_html=html` shortcut is broken in 0.9.1** — live-tested and confirmed: it doesn't parse `raw_html` at all, just returns the literal string `"html"`. `_run_crawl4ai_sync` (`scrapers/website.py`) now writes the HTML to a temp file and crawls it via a `file://` URL instead — live-verified against a real site (hitchki.co), produces real markdown. A length guard (`> 20` chars) in `_extract_markdown` falls back to `markdownify` if Crawl4AI ever silently returns near-empty output again. Crawl4AI's own markdown also keeps raw `[text](url)` link syntax (`markdownify`'s fallback strips anchors via `strip=['a']`, Crawl4AI's doesn't) — stripped with a regex in `_parse_html` before use, since it was skewing both the readability score and the AI's homepage-text context with nav-link noise.
- **Wappalyzer swapped to `wappalyzer` (wappalyzer-next, https://github.com/s0md3v/wappalyzer-next)**, replacing the stale `python-Wappalyzer`. Uses `scan_type="fast"` (single HTTP request, no browser) — live-tested at ~5s against wordpress.org, correctly detected WordPress/PHP/MySQL/Nginx/etc. Still runs off-thread via `asyncio.to_thread` with a 10s outer timeout, degrading to `[]` on timeout/error. **Do not install both `python-Wappalyzer` and `wappalyzer` in the same environment**: on case-insensitive filesystems (Windows, default macOS) their package directories (`Wappalyzer/` vs `wappalyzer/`) collide and corrupt both installs — hit this firsthand during this session, confirmed by uninstalling both and reinstalling `wappalyzer` alone. `requirements.txt` only lists the new one, so a fresh `pip install -r requirements.txt` is fine; this only bites if you `pip install python-Wappalyzer` back in manually.
- **`pyseoanalyzer` makes its own separate HTTP fetch of the target URL** (not the Playwright HTML already in hand) — accepted tradeoff (see §5.6) for real crawl-based checks (word count, page warnings). Adds a few seconds to each audit; `follow_links=False` keeps it to just the one page. Its extracted `description` field can occasionally pick up garbage (live-tested on a messy real site, it once flagged a video file URL as "the description is too long") — surfaced as-is in the flaw list since filtering every possible garbage pattern isn't worth the complexity; treat pyseoanalyzer warnings as generally useful but not 100% clean signal.
- **Two storage backends, two schemas**: SQLite (`storage/db.py`, snake_case columns) and Google Sheets (`storage/sheets.py`, Title Case header strings as field keys) are separate, unreconciled data stores — Sheets is the lead/CRM source of truth (`get_pending_leads`, `find_row_by_website`, etc.), SQLite is cost/history/drafts only. Not touched in this pass; if scope grows, decide whether to retire one (e.g. Baserow, MIT-licensed self-hosted Airtable alternative, was considered but not implemented — needs a deployed instance first).
- **Rate limiting** is in place on all `/api/*` routes (`app.py:rate_limit`, in-memory sliding window keyed by API key, `429` on breach): 5/min on `/api/search` and `/api/audit`, 10/min on `/api/send`, 30/min on the drafts delete, 120/min on the three GET/poll endpoints (`/api/costs` is polled every 5s by the frontend, so it needs headroom). In-memory only — fine for one Railway instance, won't hold up if this ever runs multi-instance/multi-worker (would need Redis or similar for a shared counter).
- **Instagram scraping (`instagrapi`) and the Google Maps Playwright fallback both scrape platforms directly against their ToS.** This session added mitigations, not a fix — the underlying ToS/ban risk doesn't go away:
  - `scrapers/instagram.py`: added a circuit breaker (`_trip_challenge_breaker`) — a `ChallengeRequired` from Instagram now pauses *all* Instagram calls for 60 minutes (`_CHALLENGE_COOLDOWN_SECONDS`) instead of retrying immediately, which is what turns a soft flag into a permanent ban. `_ensure_logged_in` checks the cooldown before attempting any login.
  - `scrapers/google_maps.py`: jittered delays (was fixed `sleep(1.5)`/`sleep(3)`, now randomized ranges) on the Maps scroll loop, plus a jittered delay + `asyncio.to_thread` between each DDG website-lookup call (was a tight zero-delay loop hammering DDG synchronously on the event loop).
  - A dedicated scraper repo (e.g. gosom/google-maps-scraper) was considered for the Maps side but skipped — it's written in Go, not Python, bigger integration than warranted right now.

---

## 9. Database Schema (`storage/db.py`)

SQLite at `data/database.sqlite` (repo-root-relative, auto-created). Every public function (`log_cost`, `log_email`, `get_costs`, etc.) calls `init_db()` and opens/closes its own `sqlite3.connect()` — no shared connection or pool.

- **`cost_logs`**: `id` (PK autoincrement), `timestamp` (default `CURRENT_TIMESTAMP`), `category` (TEXT NOT NULL — e.g. `"Google Maps API"`, `"AI Audit"`, `"AWS SES"`), `cost` (REAL NOT NULL), `description` (TEXT)
- **`email_history`**: `id` (PK), `timestamp`, `company`, `website`, `target_email`, `sender_email`, `subject`, `body`
- **`email_drafts`**: `id` (PK), `timestamp`, `company`, `website`, `target_email`, `subject`, `body`, `image_url`

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

Railway builds from **`Dockerfile`** (`railway.json` pins `"builder": "DOCKERFILE"`). `nixpacks.toml` exists in the repo but is **not used** — it's leftover from an earlier setup; don't edit it expecting it to affect the deploy.

- `Dockerfile` is a 3-stage build: a Python deps stage, a Node `frontend-builder` stage that runs `npm run build` fresh on every deploy, and a final Python+Playwright runtime image that copies both in. Playwright's Chromium system deps (`libnss3`, `libatk1.0-0`, etc.) are installed via `apt-get` in the final stage; `playwright install chromium` downloads the browser itself.
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
