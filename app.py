import asyncio
import os
import random
import time
from collections import defaultdict, deque
from fastapi import FastAPI, HTTPException, BackgroundTasks, Header, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
from scrapers.google_maps import GoogleMapsScraper
from scrapers.website import WebsiteScraper
from scrapers.instagram import InstagramScraper
from analyzer.ai_audit import AIAuditor
from emailer.ses_sender import SESSender
from enrichment.decision_maker import DecisionMaker
from analyzer.visuals import generate_audit_screenshot, make_screenshot_filename
from storage.sheets import SheetsStorage
from storage import db
from security_utils import validate_public_url, UnsafeURLError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCREENSHOTS_DIR = os.path.join(BASE_DIR, "data", "screenshots")

app = FastAPI(title="Lead Audit Bot Web App")

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)
if config.ALLOWED_ORIGINS == ["*"]:
    print("[CORS] WARNING: ALLOWED_ORIGINS not set — allowing all origins. Set ALLOWED_ORIGINS in production.")

if not config.API_KEY:
    print(
        "[Auth] WARNING: API_KEY is not set — every /api/* endpoint is unauthenticated. "
        "Set API_KEY in your environment before deploying anywhere reachable from the internet."
    )


async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """
    Gate on X-API-Key. No-op (open) if API_KEY isn't configured, so local dev
    without a .env still works — but that means auth is OFF until you set it.
    """
    if not config.API_KEY:
        return
    if x_api_key != config.API_KEY:
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key header")


# In-memory sliding-window rate limiter, keyed by API key (or "anonymous" if
# API_KEY isn't set). Single-process/in-memory is fine for a single Railway
# instance; won't hold up across multiple instances/workers.


def rate_limit(max_calls: int, window_seconds: int):
    """
    Dependency factory: allow at most *max_calls* requests per *window_seconds* per API key.

    Each call to this factory gets its OWN bucket store (closure-local, not a
    shared module-level dict) — otherwise every route using rate_limit() would
    share the same counters regardless of their different limits, and a route
    polled frequently (e.g. /api/costs every 5s) would exhaust the budget for
    an unrelated, much-stricter-limited route (e.g. /api/search at 5/min).
    """
    buckets: dict[str, deque] = defaultdict(deque)

    async def _check(x_api_key: str | None = Header(default=None)) -> None:
        key = x_api_key or "anonymous"
        bucket = buckets[key]
        now = time.monotonic()
        while bucket and now - bucket[0] > window_seconds:
            bucket.popleft()
        if len(bucket) >= max_calls:
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded: max {max_calls} requests per {window_seconds}s on this endpoint",
            )
        bucket.append(now)
    return _check


# Short-TTL cache for /api/audit results keyed by normalized website URL —
# guards against accidental duplicate audits (double-clicks, re-opening the
# same lead) re-running the full scrape+AI pipeline and burning cost/time.
_AUDIT_CACHE_TTL = 600
_audit_cache: dict[str, tuple[float, dict]] = {}


def _audit_cache_key(website: str) -> str:
    return website.strip().lower().rstrip("/")


def _audit_cache_get(website: str) -> dict | None:
    entry = _audit_cache.get(_audit_cache_key(website))
    if not entry:
        return None
    ts, data = entry
    if time.monotonic() - ts > _AUDIT_CACHE_TTL:
        return None
    return data


def _audit_cache_set(website: str, data: dict) -> None:
    _audit_cache[_audit_cache_key(website)] = (time.monotonic(), data)


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
async def search_leads(
    req: SearchRequest,
    background_tasks: BackgroundTasks,
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(5, 60)),
):
    try:
        leads = await maps_scraper.scrape_google_maps(req.niche, req.city, limit=req.limit)
        background_tasks.add_task(save_leads_to_sheets_bg, leads)

        # Log exact Maps API cost
        total_search_cost = sum(lead.get("search_cost", 0) for lead in leads)
        if total_search_cost > 0:
            await asyncio.to_thread(
                db.log_cost, "Google Maps API", total_search_cost,
                description=f"Search: {req.niche} in {req.city} ({len(leads)} leads)"
            )

        return {"leads": leads}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/audit")
async def audit_lead(
    req: AuditRequest,
    background_tasks: BackgroundTasks,
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(5, 60)),
):
    if req.website:
        try:
            await asyncio.to_thread(validate_public_url, req.website)
        except UnsafeURLError as e:
            raise HTTPException(status_code=400, detail=f"Refusing to audit this URL: {e}")

        cached = _audit_cache_get(req.website)
        if cached is not None:
            return cached

    try:
        # Check SES quota (optional but good for safety)
        quota = await asyncio.to_thread(ses.check_ses_quota)
        remaining_quota = quota.get('Max24HourSend', 0) - quota.get('SentLast24Hours', 0)
        if remaining_quota <= 0:
            return {"error": "SES quota exceeded."}

        # 1. Grab Screenshot, HTML, and run Playwright-based audits (axe-core, broken links, perf timing)
        image_path = None
        html_content = None
        extra_audit_data = None
        if req.website:
            image_path, html_content, extra_audit_data = await generate_audit_screenshot(req.website, req.company)

        # 2. Website Audit (using fully rendered HTML + Playwright audit data)
        web_data = await web_scraper.audit_website(req.website, html=html_content, extra_audit_data=extra_audit_data)

        # 3. Instagram Data — use handle from request, or auto-detect from website
        ig_handle = req.instagram_handle
        if not ig_handle and web_data.instagram_url:
            # Extract handle from URL like https://instagram.com/hitchki
            import re
            match = re.search(r'instagram\.com/([A-Za-z0-9_.]+)', web_data.instagram_url)
            if match:
                ig_handle = match.group(1)
                print(f"[Audit] Auto-detected Instagram handle from website: @{ig_handle}")

        ig_data = None
        if ig_handle:
            ig_data = await asyncio.to_thread(ig_scraper.get_instagram_data, ig_handle)

        # 4. AI Audit (with visual critique)
        analysis = await asyncio.to_thread(auditor.analyze_lead, req.company, ig_data, web_data, image_path=image_path)

        image_url = None
        if image_path:
            image_url = f"/screenshots/{os.path.basename(image_path)}"

        if not analysis:
            return {"error": "AI failed to analyze."}

        # 5. Find Contact (using fully rendered HTML)
        dm = await asyncio.to_thread(decision_maker.find_decision_maker, req.company, req.website, html_content=html_content)
        contact = dm.get("name", "")
        email = dm.get("email", "")

        dm_cost = dm.get("cost", 0.0)
        if dm_cost:
            await asyncio.to_thread(db.log_cost, "AI Web Fetch", dm_cost, description=f"Contact discovery for {req.company}")

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
        await asyncio.to_thread(db.log_cost, "AI Audit", ai_cost, description=f"Audit for {req.company}")

        # Save to DB Drafts
        await asyncio.to_thread(
            db.log_draft,
            company=req.company,
            website=req.website,
            target_email=email,
            subject=subject,
            body=body,
            image_url=image_url or ""
        )

        result = {
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
        if req.website:
            _audit_cache_set(req.website, result)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/send")
async def send_email(
    req: SendRequest,
    background_tasks: BackgroundTasks,
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(10, 60)),
):
    try:
        # Use existing screenshot (same collision-safe name generate_audit_screenshot wrote)
        image_path = None
        if req.company and req.website:
            candidate_path = os.path.join(SCREENSHOTS_DIR, make_screenshot_filename(req.company, req.website))
            if os.path.exists(candidate_path):
                image_path = candidate_path

        message_id = await asyncio.to_thread(ses.send_email, req.email, req.subject, req.body, image_path=image_path)
        success = bool(message_id)

        if success:
            def save_send_to_sheets():
                try:
                    row = sheets.find_row_by_website(req.website)
                    if row:
                        sheets.update_status(row, "emailed")
                        sheets.set_message_id(row, message_id)
                except Exception as e:
                    print(f"Error updating send status in sheets: {e}")
            background_tasks.add_task(save_send_to_sheets)

            # Log exact costs and email history
            await asyncio.to_thread(db.log_cost, "AWS SES", 0.0001, description=f"Email to {req.email}")
            await asyncio.to_thread(db.log_email, req.company, req.website, req.email, config.FROM_EMAIL, req.subject, req.body, message_id=message_id)

            # Remove from drafts since it's sent
            await asyncio.to_thread(db.delete_draft_by_website, req.website)

            return {"status": "success"}
        else:
            raise HTTPException(status_code=400, detail=f"{req.email} is on the unsubscribe/suppression list")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Deliberately unauthenticated (no require_api_key/rate_limit) and public at a
# stable path — this is what SESSender._unsubscribe_headers() puts in the
# List-Unsubscribe header, and RFC 8058 one-click unsubscribe requires mail
# clients to be able to POST here with no auth and no confirmation step.
@app.api_route("/unsubscribe", methods=["GET", "POST"])
async def unsubscribe(request: Request, email: str = ""):
    if not email:
        raise HTTPException(status_code=400, detail="Missing email")

    await asyncio.to_thread(db.add_suppression, email, "list-unsubscribe")
    try:
        row = await asyncio.to_thread(sheets.find_row_by_email, email)
        if row:
            await asyncio.to_thread(sheets.mark_unsubscribed, row)
    except Exception as e:
        print(f"Error marking unsubscribed in sheets: {e}")

    if request.method == "POST":
        # One-click (RFC 8058): mail client, not the user, does this POST — no body needed.
        return {"status": "unsubscribed"}

    return HTMLResponse(
        "<html><body style='font-family: sans-serif; padding: 40px; text-align: center;'>"
        "<p>You've been unsubscribed and won't receive further emails from us.</p>"
        "</body></html>"
    )

@app.get("/api/costs")
async def get_costs(_auth: None = Depends(require_api_key), _rl: None = Depends(rate_limit(120, 60))):
    try:
        costs = await asyncio.to_thread(db.get_costs)
        return {"costs": costs}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/history")
async def get_history(_auth: None = Depends(require_api_key), _rl: None = Depends(rate_limit(120, 60))):
    try:
        history = await asyncio.to_thread(db.get_email_history)
        return {"history": history}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/drafts")
async def get_drafts(_auth: None = Depends(require_api_key), _rl: None = Depends(rate_limit(120, 60))):
    try:
        drafts = await asyncio.to_thread(db.get_drafts)
        return {"drafts": drafts}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/drafts/{draft_id}")
async def delete_draft(draft_id: int, _auth: None = Depends(require_api_key), _rl: None = Depends(rate_limit(30, 60))):
    try:
        await asyncio.to_thread(db.delete_draft, draft_id)
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
