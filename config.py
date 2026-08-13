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

# Where replies should land, if that isn't the sending mailbox. Defaults to
# FROM_EMAIL, so leaving this unset changes nothing.
#
# The two are worth separating: cold email lands better when it comes from a
# named human than from a role address (marketing@, info@, sales@ are
# filtered harder), but the person whose name is on it isn't necessarily the
# person who should be reading the answers — or the one whose mailbox reply
# detection can log into. A Reply-To at the same domain is completely
# ordinary; pointing it at a different domain is not, and would look like a
# phishing pattern.
REPLY_TO_EMAIL = os.getenv("REPLY_TO_EMAIL") or FROM_EMAIL

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

# Reputation warm-up sending (warmup_send.py) — off by default so a deploy
# never starts sending warm-up mail on its own. When enabled and scheduler.py
# is running as its own Railway service (app.py's web service does NOT run
# scheduler.py — see CLAUDE.md §12), this fires once daily instead of relying
# on a local machine's Task Scheduler, which only runs while that machine is
# on. Fixed cron hour rather than a full expression, matching the simple
# daily-job pattern the rest of scheduler.py already uses.
WARMUP_ENABLED = os.getenv("WARMUP_ENABLED", "false").strip().lower() in ("true", "1", "yes")
WARMUP_HOUR = int(os.getenv("WARMUP_HOUR", "13"))  # 24h, Asia/Kolkata — matches the prior local 1:00 PM schedule

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

# A draft's audit data is frozen at the moment it was generated, but drafts
# sit in the inbox until a human sends them — a site can be redesigned, fixed,
# or taken down in between, and the email would still cite the old findings as
# current fact. Past this many days /api/send refuses to send without an
# explicit acknowledgement, so stale claims need a deliberate decision rather
# than going out unnoticed. Set to 0 to disable the staleness gate entirely.
DRAFT_STALE_DAYS = int(os.getenv("DRAFT_STALE_DAYS", "7"))

# Only leads scoring BELOW this are contacted — above it the site is
# considered healthy enough not to be worth a cold email. Lives here rather
# than as a bare module constant in analyzer/ai_audit.py so it can be tuned
# per deployment like every other threshold. Note a partially-failed audit
# inflates the score (fewer detected flaws = higher score), which is exactly
# why should_contact() bypasses this entirely when partial_coverage is
# non-empty — see CLAUDE.md §8.
CONTACT_THRESHOLD = int(os.getenv("CONTACT_THRESHOLD", "70"))

# Re-check the drafted email's factual claims against the live site before the
# draft is accepted (analyzer/claim_verifier.py). Every other accuracy check
# reads the same audit data the drafting model read, so a claim that was wrong
# BEFORE the AI saw it — a mis-parsed phone number, a mis-probed link, a
# desktop measurement labelled as mobile — is invisible to all of them. This
# is the only check that goes back to the source, and it caught all three of
# those on a real lead. Costs one page fetch, up to 25 link probes and two
# PageSpeed calls per drafted lead, and no model tokens. Set to 0 to disable.
VERIFY_CLAIMS_LIVE = os.getenv("VERIFY_CLAIMS_LIVE", "1") not in ("0", "false", "False", "")

# Which copy variant outgoing emails use. Recorded on every send
# (email_history.variant) so reply rates can actually be compared — see
# db.get_variant_performance().
#   "classic" — every flaw the AI picked (3-4 paragraphs, ~220-260 words),
#               the original unevidenced "I've been helping brands..." line,
#               and a 10-minute-call ask. What was sent before 2026-08-09.
#   "short"   — the single most severe flaw only, and a lower-friction ask
#               (a list they receive, not a meeting they have to attend).
# "short" is a HYPOTHESIS about what converts better on cold email, not a
# measured fact — which is why both exist and the outcome is tracked instead
# of one silently replacing the other. Default switched "classic" -> "short"
# 2026-08-10: the founder independently reported the exact thing this
# module's own docstring already flagged as a risk — "the email is
# lengthy" — before any variant data existed to weigh in either direction.
# Given a live, specific complaint about length, defaulting to the
# ALREADY-BUILT shorter variant is the direct fix rather than waiting on
# data that was never being collected anyway (see CLAUDE.md §8: engagement
# tracking existed for weeks with every email structurally identical, so
# variant is what makes an outcome attributable to a decision at all).
# Override with EMAIL_VARIANT=classic to go back.
EMAIL_VARIANT = os.getenv("EMAIL_VARIANT", "short").strip().lower()

# data.gov.in's Open Government Data API — free, self-serve API key, no
# payment ever involved (register at https://data.gov.in, "My Account" ->
# "Generate API Key"). Feeds analyzer/mca_lookup.py: real financial data
# (paid-up/authorized capital) for companies registered with India's
# Ministry of Corporate Affairs, the strongest signal budget_signal.py can
# get, when it applies. It usually doesn't: sole proprietorships (very
# common for a single-location small business) have NO Ministry of
# Corporate Affairs record at all, so this only ever fires for leads
# registered as a Pvt Ltd/LLP. Unset by default — the lookup is skipped
# entirely (not attempted, not logged as a miss) until both this key AND
# MCA_COMPANY_MASTER_RESOURCE_ID are filled in.
DATA_GOV_IN_API_KEY = os.getenv("DATA_GOV_IN_API_KEY", "").strip()

# The specific dataset's resource ID on the OGD platform (the UUID that
# appears in https://api.data.gov.in/resource/<this>). Deliberately NOT
# hardcoded to a guessed value: resource IDs are catalog-specific and the
# data.gov.in catalog page could not be verified against a live session
# while this was written (returned 403 to an unauthenticated fetch). Find
# yours from your own data.gov.in account's Company Master Data resource
# page before setting this — a wrong ID means every lookup 404s and the
# feature silently never fires, which is safe (see mca_lookup.py's
# fail-closed design) but also silently useless until corrected.
MCA_COMPANY_MASTER_RESOURCE_ID = os.getenv("MCA_COMPANY_MASTER_RESOURCE_ID", "").strip()

# One line of real credibility placed just before the ask. The default copy
# ("I've been helping brands fix exactly these things") names no client, no
# result and no number — a stranger asserting competence, which carries
# roughly no weight. Set this to something specific and TRUE, e.g.
# "I did this for two dental clinics in Pune last month — both are now
# loading in under 2s." Left unset, the original generic line is used.
SOCIAL_PROOF_LINE = os.getenv("SOCIAL_PROOF_LINE", "")

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
