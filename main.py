"""
Main orchestration script — ties all modules together to run the lead audit bot.
"""

import asyncio
import os
import random
import sys

import config
from analyzer.ai_audit import AIAuditor
from emailer.ses_sender import SESSender
from enrichment.decision_maker import DecisionMaker
from scrapers.instagram import InstagramScraper
from scrapers.website import WebsiteScraper
from storage.sheets import SheetsStorage
from analyzer.visuals import generate_audit_screenshot

# ==============================================================================
# Initialization
# ==============================================================================

try:
    decision_maker = DecisionMaker()
    ig_scraper = InstagramScraper()
    web_scraper = WebsiteScraper()
    auditor = AIAuditor()
    ses = SESSender()
    sheets = SheetsStorage()
except Exception as e:
    print(f"Failed to initialize components: {e}")
    sys.exit(1)

YOUR_NAME = os.getenv("YOUR_NAME", "Kshitij Gupta")
emails_sent = 0

# ==============================================================================
# Core Pipeline
# ==============================================================================

async def process_single_lead(lead: dict) -> str:
    """
    Process a single lead.
    Returns: "emailed" | "skipped" | "failed_no_email" | "failed_no_website" | "failed_ai_error" | "failed_error"
    """
    company = lead.get("Company", "")
    email = lead.get("Email", "")
    contact = lead.get("Contact Name", "")
    website = lead.get("Website", "")
    instagram_handle = lead.get("Instagram Handle", "")
    
    print(f"\nProcessing: {company}")
    
    # Step 1: Find Instagram if missing
    if not instagram_handle and website:
        instagram_handle = decision_maker.find_instagram_handle(company, website)
    
    # Step 2: Scrape Instagram
    ig_data = None
    if instagram_handle:
        ig_data = ig_scraper.get_instagram_data(instagram_handle)
        print(f"  IG: {ig_data.followers if ig_data else 'not found'} followers")
    
    # Step 3: Generate Visual Evidence & Scrape HTML (Playwright)
    print(f"  Generating visual evidence & scraping for {website}...")
    image_path, html_content, extra_audit_data = await generate_audit_screenshot(website, company)
    mobile_image_path = (extra_audit_data or {}).get("mobile_image_path")

    # Step 4: Find decision maker if name or email missing (using Playwright HTML)
    if (not contact or not email) and website:
        dm = decision_maker.find_decision_maker(company, website, html_content=html_content)
        contact = dm.get("name", "")
        email = dm.get("email", "")

    if not email:
        print(f"  No email found for {company}")
        return "failed_no_email"

    # Step 5: Audit website (with Playwright HTML)
    web_data = await web_scraper.audit_website(website, html=html_content, extra_audit_data=extra_audit_data)
    print(f"  Web: speed={web_data.page_speed_score}, seo={web_data.seo_score}")

    # Step 6: AI Analysis
    # "Rating"/"Reviews Count" only come from Google Maps leads and can be
    # "N/A" (the Playwright OSINT fallback in scrapers/google_maps.py has no
    # rating data) — guard against feeding that literal string to the prompt
    # or crashing int() on a non-numeric value.
    raw_rating = str(lead.get("Rating", "")).strip()
    rating = raw_rating if raw_rating.replace(".", "", 1).isdigit() else ""
    try:
        reviews_count = int(lead.get("Reviews Count") or 0)
    except (TypeError, ValueError):
        reviews_count = 0

    analysis = auditor.analyze_lead(
        company, ig_data, web_data,
        image_path=image_path,
        mobile_image_path=mobile_image_path,
        rating=rating,
        reviews_count=reviews_count,
    )
    if not analysis:
        print(f"  AI audit failed for {company}")
        return "failed_ai_error"
    
    # Step 7: Skip if score is too good (not worth emailing)
    if analysis.get("overall_score", 100) > 70:
        print(f"  Score {analysis.get('overall_score')} — too good, skipping")
        return "skipped"
    
    # Step 8: Generate and draft email
    subject, body = ses.generate_email(company, contact, analysis, YOUR_NAME)
    
    print(f"  Drafting email for {company}...")
    if "row_number" in lead:
        sheets.save_draft(lead["row_number"], subject, body)
        
    # Clean up the screenshot files to save space
    for path in (image_path, mobile_image_path):
        if path:
            try:
                os.remove(path)
            except OSError:
                pass

    return "drafted"


# ==============================================================================
# Batch Runner
# ==============================================================================

# Delay between sends within a batch, jittered — a fresh sending domain
# blasting its whole daily quota back-to-back in seconds reads as automated
# bulk mail to Gmail regardless of DAILY_EMAIL_LIMIT's cap on total volume;
# spacing sends out looks like organic, human-paced outreach instead. Same
# "looks automated" concern that's why scrapers/google_maps.py already
# jitters its own delays. Only applies between successive real sends, not
# every lead processed (skips/failures don't need to be throttled).
_MIN_SEND_DELAY_SECONDS = 60
_MAX_SEND_DELAY_SECONDS = 240


async def run_batch():
    """Run a batch of emails for pending leads."""
    global emails_sent
    emails_sent = 0

    print("--- Starting Lead Audit Bot Batch ---")
    
    # Check SES quota first
    quota = ses.check_ses_quota()
    remaining_quota = quota.get('Max24HourSend', 0) - quota.get('SentLast24Hours', 0)
    print(f"SES quota remaining today: {remaining_quota}")
    
    if remaining_quota <= 0:
        print("No SES quota remaining today. Exiting.")
        return
        
    leads = sheets.get_pending_leads()[:config.DAILY_EMAIL_LIMIT]
    print(f"Pending leads in CRM (batch capped at DAILY_EMAIL_LIMIT={config.DAILY_EMAIL_LIMIT}): {len(leads)}")
    
    for lead in leads:
        if emails_sent >= config.DAILY_EMAIL_LIMIT:
            print(f"Internal daily limit ({config.DAILY_EMAIL_LIMIT}) reached. Stopping batch.")
            break
            
        if emails_sent >= remaining_quota:
            print(f"AWS SES quota limit reached. Stopping batch.")
            break
        
        try:
            result = await process_single_lead(lead)
        except Exception as e:
            print(f"  Error processing lead {lead.get('Company')}: {e}")
            result = "failed_error"
            
        # Update row in Google Sheets
        if "row_number" in lead:
            sheets.update_status(lead["row_number"], result)
        
        if result == "emailed":
            emails_sent += 1
            print(f"  Result: {result} | Total sent today: {emails_sent}")
            if emails_sent < config.DAILY_EMAIL_LIMIT:
                delay = random.uniform(_MIN_SEND_DELAY_SECONDS, _MAX_SEND_DELAY_SECONDS)
                print(f"  Waiting {delay:.0f}s before next send (avoids a burst-of-mail pattern)...")
                await asyncio.sleep(delay)
        else:
            print(f"  Result: {result} | Total sent today: {emails_sent}")
    
    stats = sheets.get_stats()
    print(f"\nBatch done! Current CRM stats: {stats}")

async def run_followups():
    """Run follow-up drip campaigns."""
    print("--- Starting Follow-up Batch ---")
    
    leads = sheets.get_leads_for_followup(max_stage=2)
    print(f"Leads ready for follow-up: {len(leads)}")
    
    for lead in leads:
        company = lead.get("Company", "")
        contact = lead.get("Contact Name", "")
        email = lead.get("Email", "")
        subject = lead.get("Email Subject", "")
        stage = lead.get("Follow-up Stage", 0)
        original_message_id = lead.get("Message ID", "")

        if not email or not subject:
            continue

        new_stage = int(stage) + 1
        print(f"Sending Follow-up {new_stage} to {company} ({email})")

        body = ses.generate_followup(contact, new_stage, YOUR_NAME)
        success = ses.send_followup(email, subject, body, in_reply_to=original_message_id)
        
        if success and "row_number" in lead:
            sheets.increment_followup(lead["row_number"], new_stage)
            
    print("--- Follow-up Batch Complete ---")


if __name__ == "__main__":
    asyncio.run(run_batch())
