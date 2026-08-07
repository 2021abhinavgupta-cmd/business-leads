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

# Which transport actually delivers the mail: "ses" (default) or "gmail".
# Gmail here means an authenticated Google Workspace mailbox over SMTP —
# Gmail treats mail from its own infrastructure differently from third-party
# ESP traffic, and this tool's leads are mostly Gmail inboxes. See
# emailer/gmail_sender.py for the two real constraints before switching.
EMAIL_PROVIDER = os.getenv("EMAIL_PROVIDER", "ses")

# Workspace mailbox used when EMAIL_PROVIDER=gmail. GMAIL_APP_PASSWORD is an
# App Password (https://myaccount.google.com/apppasswords), not the account
# password — creating one requires 2-Step Verification on the account.
GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")

# Self-imposed daily ceiling for the Gmail transport. Google's own limit is
# far higher (2,000 external recipients/day on Workspace), but the risk being
# managed here is account suspension for bulk unsolicited mail, not the hard
# limit — so this stays deliberately low.
GMAIL_DAILY_CAP = int(os.getenv("GMAIL_DAILY_CAP", "40"))

# Embed a 1x1 tracking pixel in outgoing HTML so opens can be logged.
# OFF by default and deliberately so: a remote image in a first-touch email
# from an unknown sender is a spam signal, which is a real cost paid for a
# number that can't be trusted (Apple Mail pre-fetches every image, so opens
# read high; images-off readers register nothing, so they read low). Needs
# APP_BASE_URL set — a relative URL is meaningless inside an email.
# See emailer/tracking.py.
EMAIL_OPEN_TRACKING = os.getenv("EMAIL_OPEN_TRACKING", "false").strip().lower() in ("true", "1", "yes")

# === Reply detection (IMAP) ===
# The mailbox replies land in — every outgoing message sets Reply-To to
# FROM_EMAIL, so this is that mailbox regardless of which transport sent it.
# IMAP_PASSWORD must be an App Password for Gmail/Workspace, not the account
# password. Scanning is read-only (BODY.PEEK) and never marks mail as read.
IMAP_HOST = os.getenv("IMAP_HOST", "imap.gmail.com")
IMAP_USER = os.getenv("IMAP_USER")
IMAP_PASSWORD = os.getenv("IMAP_PASSWORD")

# How far back each scan looks. Generous by default: re-seeing a reply is a
# no-op (email_replies is keyed on the reply's own Message-ID), whereas a
# window shorter than the gap between scans loses replies permanently.
REPLY_LOOKBACK_DAYS = int(os.getenv("REPLY_LOOKBACK_DAYS", "30"))

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

# Self-consistency: generate the audit copy twice and keep only the claims
# whose cited source line appears in BOTH runs. A claim the model invents
# tends not to survive a second independent sample, so agreement across runs
# is real evidence of grounding in a way a single low-temperature run isn't.
# Costs one extra AI call per lead (roughly doubles per-lead AI spend, which
# is fractions of a cent on Haiku/Flash) and adds a few seconds. Set
# AI_SELF_CONSISTENCY=false to disable.
AI_SELF_CONSISTENCY = os.getenv("AI_SELF_CONSISTENCY", "true").strip().lower() not in ("false", "0", "no")

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
