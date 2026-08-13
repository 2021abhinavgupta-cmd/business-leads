"""
Scheduler — runs the lead ingestion and email batch processes automatically.
"""

import asyncio
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from main import run_batch, run_followups
from scrapers.google_maps import GoogleMapsScraper
from scrapers.shopify_dork import ShopifyDorkScraper
from scrapers.startup_dork import StartupDorkScraper
from scrapers.apollo_free import ApolloFreeScraper
from storage.sheets import SheetsStorage
from warmup_send import run_warmup
from emailer.reply_checker import check_replies
import config


def _safe_check_replies():
    """
    Wrapped so one bad IMAP session (a transient auth hiccup, a network
    blip) logs and moves on rather than leaving the day's reply-check
    silently skipped with no trace — and, more importantly, never crashes
    the scheduler process itself, which would take the follow-up and
    batch-send jobs down with it.
    """
    try:
        summary = check_replies()
        print(f"[Replies] Daily check: {summary}")
    except Exception as e:
        print(f"[Replies] Daily check failed (non-fatal): {e}")


async def ingest_leads():
    """
    Scrape leads from Google Maps and Apollo and append them to the CRM.
    """
    print("--- Starting Lead Ingestion ---")

    try:
        sheets = SheetsStorage()

        # Select scraper based on LEAD_SOURCE in config
        source = config.LEAD_SOURCE
        if source == "ecommerce":
            scraper = ShopifyDorkScraper()
            niches = ["skincare", "apparel", "jewelry", "home decor", "supplements"]
        elif source == "startups":
            scraper = StartupDorkScraper()
            niches = ["ai", "fintech", "saas", "healthtech", "edtech"]
        elif source == "b2b":
            scraper = ApolloFreeScraper()
            niches = ["software", "marketing agency", "recruiting", "consulting"]
        else: # Default to maps
            scraper = GoogleMapsScraper()
            niches = [
                ("digital marketing agency", "Mumbai"),
                ("d2c brand", "Mumbai"),
                ("restaurant", "Mumbai"),
                ("fashion brand", "Mumbai")
            ]

    except Exception as e:
        print(f"Failed to initialize ingestion components: {e}")
        return

    # Ingest from selected source
    for niche in niches:
        if source == "maps":
            niche_query, city = niche
            print(f"Scraping Maps: {niche_query} in {city}...")
            leads = await scraper.scrape_google_maps(niche_query, city, limit=50)
        else:
            print(f"Scraping {source}: {niche}...")
            leads = scraper.scrape(niche, limit=50)

        for lead in leads:
            sheets.add_lead(lead)

    print("--- Lead Ingestion Complete ---")


def start_scheduler():
    """
    Start the blocking APScheduler.
    """
    scheduler = BlockingScheduler(timezone="Asia/Kolkata")
    
    # 1) Send emails Mon-Fri at 10:00 AM IST
    scheduler.add_job(
        lambda: asyncio.run(run_batch()),
        CronTrigger(day_of_week="mon-fri", hour=10, minute=0),
        name="run_batch_weekdays"
    )
    
    # 1b) Check for replies Mon-Fri at 8:55 AM IST, just before follow-ups
    # run. run_followups() has no way to know a lead already replied unless
    # this has run first — check_replies() is the only thing that marks a
    # lead's Sheet row "replied" (see emailer/reply_checker.py), and without
    # it a follow-up sequence would happily mail someone who already said
    # yes or no. Gated on IMAP actually being configured so this doesn't
    # fire (and log a failure every single day) on a deployment that never
    # set it up — matches the WARMUP_ENABLED gating pattern just below.
    if config.IMAP_HOST and config.IMAP_USER and config.IMAP_PASSWORD:
        scheduler.add_job(
            _safe_check_replies,
            CronTrigger(day_of_week="mon-fri", hour=8, minute=55),
            name="check_replies_weekdays",
        )
        print("Reply checking enabled: daily at 08:55 IST, before follow-ups.")
    else:
        print("Reply checking NOT configured (IMAP_HOST/IMAP_USER/IMAP_PASSWORD) — "
              "follow-ups cannot detect replies automatically until this is set.")

    # 2) Send follow-ups Mon-Fri at 9:00 AM IST
    scheduler.add_job(
        lambda: asyncio.run(run_followups()),
        CronTrigger(day_of_week="mon-fri", hour=9, minute=0),
        name="run_followups_weekdays"
    )
    
    # 2) Ingest new leads every Sunday at 8:00 PM IST
    scheduler.add_job(
        lambda: asyncio.run(ingest_leads()),
        CronTrigger(day_of_week="sun", hour=20, minute=0),
        name="ingest_leads_sunday"
    )

    # 3) Reputation warm-up send, once daily — opt-in via WARMUP_ENABLED so
    # deploying this scheduler doesn't silently start sending warm-up mail.
    # This replaces a local Task Scheduler entry that only fired while that
    # machine happened to be on (see CLAUDE.md §13, 2026-08-06 entry) — this
    # process running at all already requires being deployed as its own
    # always-on Railway service (see config.py's WARMUP_ENABLED comment and
    # CLAUDE.md §12), which is the actual fix for that reliability gap.
    if config.WARMUP_ENABLED:
        scheduler.add_job(
            run_warmup,
            CronTrigger(hour=config.WARMUP_HOUR, minute=0),
            name="warmup_send_daily",
        )
        print(f"Warm-up sending enabled: daily at {config.WARMUP_HOUR:02d}:00 IST.")

    print("Scheduler running (Asia/Kolkata timezone). Press Ctrl+C to exit.")
    scheduler.start()


if __name__ == "__main__":
    start_scheduler()
