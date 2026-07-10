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

# === AWS Credentials (SES) ===
AWS_ACCESS_KEY = os.getenv("AWS_ACCESS_KEY")
AWS_SECRET_KEY = os.getenv("AWS_SECRET_KEY")
AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")

# === Email ===
FROM_EMAIL = os.getenv("FROM_EMAIL")

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
DAILY_EMAIL_LIMIT = int(os.getenv("DAILY_EMAIL_LIMIT", "100"))
LEAD_SOURCE = os.getenv("LEAD_SOURCE", "maps").lower() # options: maps, ecommerce, startups, b2b

# === API Auth ===
# Required header (X-API-Key) for all /api/* routes. If unset, the API is
# wide open — set this before deploying anywhere reachable from the internet.
API_KEY = os.getenv("API_KEY")
