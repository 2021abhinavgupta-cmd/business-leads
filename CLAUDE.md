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
│   ├── lighthouse.py          # Lighthouse CLI runner for performance scores
│   └── visuals.py             # Playwright screenshots, axe-core audits, precise Pillow bounding boxes
├── emailer/
│   ├── __init__.py
│   └── ses_sender.py          # SESSender — AWS SES sending with attachments
├── storage/
│   ├── __init__.py
│   ├── db.py                  # SQLite Persistent Database (History, Costs, Drafts)
│   └── sheets.py              # SheetsStorage — gspread CRUD (Legacy CRM)
├── frontend/                  # React + Vite Frontend UI
├── data/                      # Persistent storage mount point (database.sqlite)
├── screenshots/               # Temporary generated AI audit screenshots
├── requirements.txt           # Python dependencies
├── package.json               # Node.js dependencies (Lighthouse)
├── nixpacks.toml              # Railway multi-language (Python + Node) build configuration
├── railway.json               # Railway deployment configuration
├── .env                       # Secret keys (never commit)
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
| **AI Vision**   | `Pillow` (PIL)                             |
| **AI Models**   | `google-generativeai`, `openai`, `anthropic` |
| **Email**       | `boto3` (AWS SES)                          |
| **Database**    | `sqlite3`                                  |

**Installation:**
Both Python and Node.js are required.
- Install Backend: `pip install -r requirements.txt; playwright install chromium`
- Install Lighthouse: `npm install` (in root directory)
- Install Frontend: `cd frontend && npm install`

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

---

## 5. Core Modules

### 5.1 `app.py` (FastAPI Server)
- Serves the compiled React frontend `dist/` statically.
- Exposes REST endpoints: `/api/search`, `/api/audit`, `/api/send`, `/api/drafts`, `/api/costs`, `/api/history`.
- Connects the AI orchestration pipeline to the database and frontend.

### 5.2 `scrapers/website.py` & Deep Crawling
- Uses `Crawl4AI` to extract extremely clean LLM-optimized markdown from the homepage.
- Uses `Jina Reader` and `Trafilatura` to deep-crawl context pages (About Us, Services).
- Aggregates Lighthouse scores, PageSpeed fallbacks, and broken link data.

### 5.3 `analyzer/lighthouse.py`
- Runs the Google Lighthouse CLI via a Node.js subprocess to gather `performance`, `seo`, `accessibility`, and `best_practices` scores natively.

### 5.4 `analyzer/visuals.py`
- Headless Playwright script running a standard Desktop (1280x800) viewport.
- **Interconnected Red Box Pipeline**:
  1. Runs `axe-core` accessibility audit.
  2. Extracts the exact bounding box `(x, y, w, h)` of the most severe UI violation.
  3. Draws a precise red box around the real flaw using `Pillow`.
  4. Passes the `visual_flaw_context` up the chain to the AI prompt.

### 5.5 `analyzer/ai_audit.py`
- Ingests `company_context` to force extreme outreach personalization.
- Takes in the `visual_flaw_context` and is strictly instructed to explicitly mention the red box drawn on the screenshot in the email body.
- Uses real Lighthouse metrics to quote exact load times and SEO scores.

---

## 6. How To Run Locally

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

## 7. Deployment (Railway)

The app is fully configured for zero-downtime deployment on Railway using Nixpacks.
- `nixpacks.toml` provisions **both Python and Node.js** environments dynamically so that Lighthouse CLI and Playwright can run side-by-side.
- Railway mounts a persistent volume at `/app/data` to ensure `database.sqlite` and `screenshots/` survive deployments.

---

## 8. Changelog

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
