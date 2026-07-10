# CLAUDE.md — Lead Audit Bot

> **MANDATORY**: Read this file in full before writing or modifying any code in this project.

---

## 1. Project Overview

**lead-audit-bot** is an automated B2B lead-generation and AI audit pipeline that operates as a modern Full-Stack Web App. It features:

1. **React Frontend**: A sleek, multi-page Dashboard with Top Navigation (Dashboard, Drafts, Costs, History).
2. **FastAPI Backend**: Serves the API and connects the web scrapers, AI analyzers, and persistent storage.
3. **Deep Web Crawling**: Uses BeautifulSoup and Trafilatura to extract deep brand context (Services, About Us) for extreme email personalization.
4. **AI Visual Auditing**: Captures Playwright mobile screenshots of leads' websites, draws bounding boxes, and forces the AI to critique the visual design.
5. **Exact Cost Tracking**: Tracks fractional penny costs (to 5 decimal places) for API usage directly in an SQLite database.
6. **Asynchronous Drafts Inbox**: Allows users to run Autopilot, queue up generated AI emails in a Drafts table, and manually review/send them via AWS SES at their leisure.

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
│   └── website.py             # WebsiteScraper — Playwright + httpx + Trafilatura
├── enrichment/
│   ├── __init__.py
│   └── decision_maker.py      # DuckDuckGo OSINT & Email Validation
├── analyzer/
│   ├── __init__.py
│   ├── ai_audit.py            # AIAuditor — multi-provider AI reports
│   └── visuals.py             # Playwright screenshot capture & Pillow bounding boxes
├── emailer/
│   ├── __init__.py
│   └── ses_sender.py          # SESSender — AWS SES sending with attachments
├── storage/
│   ├── __init__.py
│   ├── db.py                  # SQLite Persistent Database (History, Costs, Drafts)
│   └── sheets.py              # SheetsStorage — gspread CRUD (Legacy CRM)
├── frontend/                  # React + Vite Frontend UI
│   ├── src/
│   │   ├── App.jsx            # Main React UI Layout & API Integration
│   │   ├── App.css            # Custom glassmorphism UI styles
│   │   └── main.jsx
│   └── package.json
├── data/                      # Persistent storage mount point (database.sqlite)
├── screenshots/               # Temporary generated AI audit screenshots
├── requirements.txt
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
| **Scraping**    | `httpx`, `beautifulsoup4`, `trafilatura`   |
| **Headless**    | `playwright`                               |
| **AI Vision**   | `Pillow` (PIL)                             |
| **AI Models**   | `google-generativeai`, `openai`, `anthropic` |
| **Email**       | `boto3` (AWS SES)                          |
| **Database**    | `sqlite3`                                  |

Install Backend: `pip install -r requirements.txt; playwright install chromium`
Install Frontend: `cd frontend && npm install`

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
| `PORT`               | Optional port mapping (Railway uses this) |

---

## 5. Core Modules

### 5.1 `app.py` (FastAPI Server)
- Serves the compiled React frontend `dist/` statically.
- Exposes REST endpoints: `/api/search`, `/api/audit`, `/api/send`, `/api/drafts`, `/api/costs`, `/api/history`.
- Connects the AI orchestration pipeline to the database and frontend.

### 5.2 `storage/db.py` (SQLite DB)
- Replaces standard JSON logs with a permanent SQLite database file (`data/database.sqlite`).
- Tracks **Cost Logs** (e.g., $0.032 for Search, $0.00014 for AI generation).
- Tracks **Email Drafts** (allowing async review).
- Tracks **Email History** (permanent outbox).

### 5.3 `scrapers/website.py` & Deep Crawling
- Uses `httpx` to ping the target homepage.
- Uses **Trafilatura** and `BeautifulSoup` to find up to 3 context pages (About Us, Services) and extract raw markdown.
- Assembles a `company_context` string of up to 3,000 words.

### 5.4 `analyzer/ai_audit.py`
- Ingests `company_context` and forces the AI to output a highly personalized opening line proving it knows what the target company sells.
- Mandates the inclusion of exactly 3 flaws (Speed, SEO, and crucially, a Visual Critique based on the screenshot).
- Dynamically parses the JSON output.

### 5.5 `analyzer/visuals.py`
- Headless Playwright script that navigates to the lead's website mimicking an iPhone 13 viewport.
- Takes a screenshot, saves it to `/screenshots/`, and draws a bounding box using Pillow to serve as visual evidence in the email.

### 5.6 `frontend/src/App.jsx`
- Sleek, premium dark-mode UI with glassmorphism effects.
- Top navigation layout for viewing the Dashboard, Saved Drafts, Lifetime Costs, and Email History.

---

## 6. How To Run Locally

```bash
# 1. Install Backend
pip install -r requirements.txt
playwright install chromium

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

The app is fully configured for zero-downtime deployment on Railway:
- `railway.json` dictates the build and start commands.
- Railway mounts a persistent volume at `/app/data` to ensure `database.sqlite` survives deployments and restarts.
- Railway manages the `PORT` variable dynamically.

---

## 8. Changelog

| Date       | Change                                              |
| ---------- | --------------------------------------------------- |
| 2026-06-24 | Initial project scaffold created. |
| 2026-07-06 | Implemented God-Tier OSINT & Playwright screenshots for visual email evidence. |
| 2026-07-09 | Refactored into a full-stack React + FastAPI web application for local and remote deployment. |
| 2026-07-10 | Added persistent SQLite database for 100% accurate fractional penny Cost Tracking and permanent Email History logging. |
| 2026-07-10 | Rebuilt UI layout to feature a Top Navigation bar. |
| 2026-07-10 | Implemented the Saved Drafts Inbox allowing users to run asynchronous Autopilot audits and review them later. |
| 2026-07-10 | Implemented the Deep Brand Context Crawler using Trafilatura to scrape "About" and "Services" pages for extreme AI personalization. |
