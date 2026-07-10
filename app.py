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
from enrichment.decision_maker import DecisionMaker
from analyzer.visuals import generate_audit_screenshot
from storage.sheets import SheetsStorage
from storage import db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCREENSHOTS_DIR = os.path.join(BASE_DIR, "data", "screenshots")

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

maps_scraper = GoogleMapsScraper()
web_scraper = WebsiteScraper()
ig_scraper = InstagramScraper()
auditor = AIAuditor()
ses = SESSender()
sheets = SheetsStorage()
decision_maker = DecisionMaker()

def save_leads_to_sheets_bg(leads: list):
    for lead in leads:
        sheet_data = {
            "Company": lead.get("Company", ""),
            "Website": lead.get("Website", ""),
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
        
        # Log exact Maps API cost
        total_search_cost = sum(lead.get("search_cost", 0) for lead in leads)
        if total_search_cost > 0:
            db.log_cost("Google Maps API", total_search_cost, description=f"Search: {req.niche} in {req.city} ({len(leads)} leads)")
            
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

        # 1. Grab Screenshot and HTML via Playwright
        image_path = None
        html_content = None
        if req.website:
            image_path, html_content = await generate_audit_screenshot(req.website, req.company)

        # 2. Website Audit (using fully rendered HTML)
        web_data = await web_scraper.audit_website(req.website, html=html_content)
        
        # 3. Instagram Data
        ig_data = None
        if req.instagram_handle:
            ig_data = ig_scraper.get_instagram_data(req.instagram_handle)

        # 4. AI Audit (with visual critique)
        analysis = auditor.analyze_lead(req.company, ig_data, web_data, image_path=image_path)
        
        image_url = None
        if image_path:
            image_url = f"/screenshots/{os.path.basename(image_path)}"
                
        if not analysis:
            return {"error": "AI failed to analyze."}

        # 5. Find Contact (using fully rendered HTML)
        dm = decision_maker.find_decision_maker(req.company, req.website, html_content=html_content)
        contact = dm.get("name", "")
        email = dm.get("email", "")

        # Generate Draft
        YOUR_NAME = os.getenv("YOUR_NAME", "Kshitij Gupta")
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
        
        # Log AI Cost
        ai_cost = analysis.get("ai_cost", 0.0001)
        db.log_cost("AI Audit", ai_cost, description=f"Audit for {req.company}")
        
        # Save to DB Drafts
        db.log_draft(
            company=req.company, 
            website=req.website, 
            target_email=email, 
            subject=subject, 
            body=body, 
            image_url=image_url or ""
        )

        return {
            "email": email,
            "sender_email": config.FROM_EMAIL,
            "subject": subject,
            "body": body,
            "page_speed_score": web_data.page_speed_score,
            "seo_score": web_data.seo_score,
            "overall_score": analysis.get("overall_score", 100),
            "flaws": analysis.get("flaws", []),
            "image_url": image_url,
            "ai_cost": analysis.get("ai_cost", 0.0001)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/send")
async def send_email(req: SendRequest, background_tasks: BackgroundTasks):
    try:
        # Use existing screenshot
        image_path = None
        if req.company:
            safe_name = "".join([c if c.isalnum() else "_" for c in req.company.lower()])
            candidate_path = os.path.join(SCREENSHOTS_DIR, f"{safe_name}_audit.jpg")
            if os.path.exists(candidate_path):
                image_path = candidate_path

        success = ses.send_email(req.email, req.subject, req.body, image_path=image_path)
        
        if success:
            def save_send_to_sheets():
                try:
                    row = sheets.find_row_by_website(req.website)
                    if row:
                        sheets.update_status(row, "emailed")
                except Exception as e:
                    print(f"Error updating send status in sheets: {e}")
            background_tasks.add_task(save_send_to_sheets)
            
            # Log exact costs and email history
            db.log_cost("AWS SES", 0.0001, description=f"Email to {req.email}")
            db.log_email(req.company, req.website, req.email, config.FROM_EMAIL, req.subject, req.body)
            
            # Remove from drafts since it's sent
            db.delete_draft_by_website(req.website)
            
            return {"status": "success"}
        else:
            raise HTTPException(status_code=500, detail="Failed to send via SES")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/costs")
async def get_costs():
    try:
        costs = db.get_costs()
        return {"costs": costs}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/history")
async def get_history():
    try:
        history = db.get_email_history()
        return {"history": history}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/drafts")
async def get_drafts():
    try:
        drafts = db.get_drafts()
        return {"drafts": drafts}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/drafts/{draft_id}")
async def delete_draft(draft_id: int):
    try:
        db.delete_draft(draft_id)
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Mount screenshots folder
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
app.mount("/screenshots", StaticFiles(directory=SCREENSHOTS_DIR), name="screenshots")

# Mount Vite frontend (for production deployment on Railway)
frontend_dist = os.path.join(os.path.dirname(__file__), "frontend", "dist")
if os.path.exists(frontend_dist):
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
