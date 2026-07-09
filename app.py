import asyncio
import os
import random
from fastapi import FastAPI, HTTPException
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

@app.post("/api/search")
async def search_leads(req: SearchRequest):
    try:
        leads = maps_scraper.scrape_google_maps(req.niche, req.city, limit=req.limit)
        return {"leads": leads}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/audit")
async def audit_lead(req: AuditRequest):
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
async def send_email(req: SendRequest):
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
            return {"status": "success"}
        else:
            raise HTTPException(status_code=500, detail="Failed to send via SES")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Mount Vite frontend (for production deployment on Railway)
frontend_dist = os.path.join(os.path.dirname(__file__), "frontend", "dist")
if os.path.exists(frontend_dist):
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
