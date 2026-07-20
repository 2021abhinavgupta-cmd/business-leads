"""
Configuration module — loads all environment variables using python-dotenv.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# === AI API Keys ===
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
# Free-tier fallback (openrouter.ai, no credit card) — only used if the three
# paid providers above all fail or run out of quota.
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# === AWS Credentials (SES) ===
AWS_ACCESS_KEY = os.getenv("AWS_ACCESS_KEY")
AWS_SECRET_KEY = os.getenv("AWS_SECRET_KEY")
AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")

# === Email ===
FROM_EMAIL = os.getenv("FROM_EMAIL")

# Public base URL of this deployment (e.g. "https://myapp.up.railway.app"),
# used to build a one-click HTTPS unsubscribe link for the List-Unsubscribe
# header (RFC 8058). If unset, outgoing emails still carry a mailto:
# unsubscribe fallback, just without one-click support.
APP_BASE_URL = os.getenv("APP_BASE_URL", "").rstrip("/")

# === Google Sheets ===
GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID")

# === Scraper API Keys ===
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
PAGESPEED_KEY = os.getenv("PAGESPEED_KEY")
APOLLO_API_KEY = os.getenv("APOLLO_API_KEY")

# === Instagram Credentials ===
IG_USERNAME = os.getenv("IG_USERNAME")
IG_PASSWORD = os.getenv("IG_PASSWORD")

# === Limits & Settings ===
# Default dropped from 100 to 15 (2026-07-20) then raised to 50 (2026-07-20,
# same day) on explicit user request, ahead of any Postmaster Tools data
# confirming reputation was actually improving — a faster ramp than the
# originally recommended +20%/few days. Deliberately still overridable via
# env var — if DAILY_EMAIL_LIMIT is already set in Railway, this code
# default won't change production behavior until that's updated too.
DAILY_EMAIL_LIMIT = int(os.getenv("DAILY_EMAIL_LIMIT", "50"))
LEAD_SOURCE = os.getenv("LEAD_SOURCE", "maps").lower() # options: maps, ecommerce, startups, b2b

# === API Auth ===
# Required header (X-API-Key) for all /api/* routes. If unset, the API is
# wide open — set this before deploying anywhere reachable from the internet.
API_KEY = os.getenv("API_KEY")

# === CORS ===
# Comma-separated list of allowed origins, e.g. "https://myapp.up.railway.app".
# Unset -> "*" (fine for local dev; the frontend is same-origin in production
# since Railway serves it from the same FastAPI app, so this normally only
# matters if you're hitting the API from a separate frontend deployment).
_allowed_origins_raw = os.getenv("ALLOWED_ORIGINS", "")
ALLOWED_ORIGINS = [o.strip() for o in _allowed_origins_raw.split(",") if o.strip()] or ["*"]
