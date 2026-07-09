import asyncio
import os
import random
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
from scrapers.google_maps import GoogleMapsScraper
from scrapers.website import WebsiteScraper
from scrapers.instagram import InstagramScraper
from analyzer.ai_audit import AIAuditor
from emailer.ses_sender import SESSender
from analyzer.visuals import generate_audit_screenshot
from storage.sheets import SheetsStorage

app = FastAPI(title="Lead Audit Bot Web App")

# Allow CORS for local dev
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class SearchRequest(BaseModel):
    niche: str
    city: str
    limit: int = 10

class AuditRequest(BaseModel):
    company: str
    website: str
    instagram_handle: str = ""

class SendRequest(BaseModel):
    email: str
    subject: str
    body: str
    company: str
    website: str

# Instantiations
maps_scraper = GoogleMapsScraper()
web_scraper = WebsiteScraper()
ig_scraper = InstagramScraper()
auditor = AIAuditor()
ses = SESSender()
sheets = SheetsStorage()

def save_leads_to_sheets_bg(leads: list):
    for lead in leads:
        sheet_data = {
            "Company": lead.get("name", ""),
            "Website": lead.get("website", ""),
            "Source": "web_search",
            "Status": "pending"
        }
        try:
            sheets.add_lead(sheet_data)
        except Exception as e:
            print(f"Error saving to sheets: {e}")

@app.post("/api/search")
async def search_leads(req: SearchRequest, background_tasks: BackgroundTasks):
    try:
        leads = maps_scraper.scrape_google_maps(req.niche, req.city, limit=req.limit)
        background_tasks.add_task(save_leads_to_sheets_bg, leads)
        return {"leads": leads}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/audit")
async def audit_lead(req: AuditRequest, background_tasks: BackgroundTasks):
    try:
        # Check SES quota (optional but good for safety)
        quota = ses.check_ses_quota()
        remaining_quota = quota.get('Max24HourSend', 0) - quota.get('SentLast24Hours', 0)
        if remaining_quota <= 0:
            return {"error": "SES quota exceeded."}

        # Website Audit
        web_data = await web_scraper.audit_website(req.website)
        
        # Instagram Data
        ig_data = None
        if req.instagram_handle:
            ig_data = ig_scraper.get_instagram_data(req.instagram_handle)

        # AI Audit
        analysis = auditor.analyze_lead(req.company, ig_data, web_data)
        if not analysis:
            return {"error": "AI failed to analyze."}

        # Find Contact (mocked or simplified for now, as DuckDuckGo was blocking)
        from enrichment.decision_maker import find_decision_maker
        dm = find_decision_maker(req.company, req.website)
        contact = dm.get("name", "")
        email = dm.get("email", "")

        # Generate Draft
        YOUR_NAME = os.getenv("YOUR_NAME", "Admin")
        subject, body = ses.generate_email(req.company, contact, analysis, YOUR_NAME)

        # Update Sheets in background
        def save_audit_to_sheets():
            try:
                row = sheets.find_row_by_website(req.website)
                if row:
                    sheets.save_draft(row, subject, body)
                    sheets.update_status(row, "drafted")
            except Exception as e:
                print(f"Error updating audit in sheets: {e}")

        background_tasks.add_task(save_audit_to_sheets)

        return {
            "email": email,
            "subject": subject,
            "body": body,
            "page_speed_score": web_data.page_speed_score,
            "seo_score": web_data.seo_score,
            "overall_score": analysis.get("overall_score", 100),
            "flaws": analysis.get("flaws", [])
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/send")
async def send_email(req: SendRequest, background_tasks: BackgroundTasks):
    try:
        # Generate Screenshot (takes time, optional)
        image_path = None
        if req.website:
            image_path = await generate_audit_screenshot(req.website, req.company)

        success = ses.send_email(req.email, req.subject, req.body, image_path=image_path)
        
        if image_path:
            try:
                os.remove(image_path)
            except:
                pass
                
        if success:
            def save_send_to_sheets():
                try:
                    row = sheets.find_row_by_website(req.website)
                    if row:
                        sheets.update_status(row, "emailed")
                except Exception as e:
                    print(f"Error updating send status in sheets: {e}")
            background_tasks.add_task(save_send_to_sheets)
            return {"status": "success"}
        else:
            raise HTTPException(status_code=500, detail="Failed to send via SES")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Mount Vite frontend (for production deployment on Railway)
frontend_dist = os.path.join(os.path.dirname(__file__), "frontend", "dist")
if os.path.exists(frontend_dist):
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
