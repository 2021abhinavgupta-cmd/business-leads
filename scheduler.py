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
import config


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
    
    print("Scheduler running (Asia/Kolkata timezone). Press Ctrl+C to exit.")
    scheduler.start()


if __name__ == "__main__":
    start_scheduler()
