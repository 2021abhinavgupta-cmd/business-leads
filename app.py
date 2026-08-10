import asyncio
import os
import random
import time
from collections import defaultdict, deque
from datetime import datetime
from fastapi import FastAPI, HTTPException, BackgroundTasks, Header, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import config
from scrapers.google_maps import GoogleMapsScraper
from scrapers.website import WebsiteScraper
from scrapers.instagram import InstagramScraper
from analyzer.ai_audit import AIAuditor
from emailer import get_sender
from emailer.tracking import TRANSPARENT_GIF, hash_ip, looks_automated
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


# Live progress for an in-flight /api/audit run, keyed by the same
# normalized URL as the cache above. /api/audit is one long blocking POST
# (a full audit is a couple of minutes now that timeouts were raised for
# accuracy), so the frontend otherwise has nothing to show but an
# indeterminate spinner for the whole run. The frontend polls
# /api/audit/progress to show which stage is actually running.
#
# In-memory only, same tradeoff as the rate limiter and the cache above:
# fine for one Railway instance, would need Redis if this ever runs
# multi-instance. Entries are set as the audit proceeds and dropped when
# it finishes, with a TTL sweep so an audit that dies mid-run can't leak
# a stale "still running" entry forever.
_AUDIT_PROGRESS_TTL = 900
_audit_progress: dict[str, tuple[float, dict]] = {}

# Ordered stages, so the UI can render "step 3 of 6" without hardcoding
# the pipeline shape in the frontend.
AUDIT_STAGES = [
    "Loading site & capturing screenshots",
    "Running technical audit (speed, SEO, accessibility)",
    "Checking social profiles",
    "Writing the audit with AI",
    "Finding the right contact",
    "Saving draft",
]


def _progress_set(website: str, stage_index: int, note: str = "") -> None:
    if not website:
        return
    # Opportunistic sweep — no background task needed for a dict this small.
    now = time.monotonic()
    for key, (ts, _) in list(_audit_progress.items()):
        if now - ts > _AUDIT_PROGRESS_TTL:
            _audit_progress.pop(key, None)

    _audit_progress[_audit_cache_key(website)] = (now, {
        "stage_index": stage_index,
        "total_stages": len(AUDIT_STAGES),
        "stage": AUDIT_STAGES[stage_index] if 0 <= stage_index < len(AUDIT_STAGES) else "",
        "note": note,
    })


def _progress_clear(website: str) -> None:
    if website:
        _audit_progress.pop(_audit_cache_key(website), None)


class SearchRequest(BaseModel):
    niche: str
    city: str
    limit: int = 10

class NearbySearchRequest(BaseModel):
    # Bounded by Pydantic rather than checked by hand: these come straight
    # from the browser's geolocation API, and an out-of-range coordinate
    # would otherwise be passed to Google as a paid request that can only
    # fail.
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    # Google rejects a radius above 50km outright.
    radius_m: int = Field(default=5000, ge=100, le=50000)
    limit: int = Field(default=10, ge=1, le=100)

class AuditRequest(BaseModel):
    company: str
    website: str
    instagram_handle: str = ""
    # Google Business Profile fields the search already returns and the
    # frontend already holds per lead. All optional so a direct/manual audit
    # (no Maps lead behind it) still works exactly as before.
    rating: str = ""
    reviews_count: int = 0
    gbp_phone: str = ""
    gbp_address: str = ""
    # Bypass the short-TTL result cache. The cache exists to stop a
    # double-click re-running the whole scrape+AI pipeline, but a DELIBERATE
    # re-audit (the retry button, or checking whether a site was fixed) wants
    # fresh data and would otherwise silently get a stale verdict back.
    force: bool = False

class SendRequest(BaseModel):
    email: str
    subject: str
    body: str
    company: str
    website: str
    # Set by the frontend only after a human has seen and dismissed the
    # draft's review warnings / staleness notice. Defaults to False so the
    # gate in /api/send fails closed: a caller that doesn't know about the
    # check can't accidentally bypass it.
    acknowledge_warnings: bool = False
    # Whether to attach the audit screenshot. Defaults to True so existing
    # callers are unaffected, but the Drafts UI can turn it off per draft —
    # the capture is not always worth sending (a mid-animation frame, a
    # carousel caught between slides, or a page our browser rendered badly),
    # and until now there was no way to send the copy without also sending
    # the picture.
    attach_screenshot: bool = True

maps_scraper = GoogleMapsScraper()
web_scraper = WebsiteScraper()
ig_scraper = InstagramScraper()
auditor = AIAuditor()
ses = get_sender()
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

@app.post("/api/search-nearby")
async def search_leads_nearby(
    req: NearbySearchRequest,
    background_tasks: BackgroundTasks,
    _auth: None = Depends(require_api_key),
    # Same 5/min as /api/search: this fans out into several billable Places
    # calls (one per business type), so it is if anything more expensive.
    _rl: None = Depends(rate_limit(5, 60)),
):
    """
    Find businesses of any type near a coordinate.

    Separate endpoint from /api/search rather than an optional mode on it:
    it takes a different Places endpoint (searchNearby, not searchText),
    needs no niche or city at all, and has genuinely different result
    characteristics (no pagination, capped per type).
    """
    try:
        leads = await maps_scraper.scrape_nearby(
            req.latitude, req.longitude, radius_m=req.radius_m, limit=req.limit
        )
        background_tasks.add_task(save_leads_to_sheets_bg, leads)

        total_search_cost = sum(lead.get("search_cost", 0) for lead in leads)
        if total_search_cost > 0:
            await asyncio.to_thread(
                db.log_cost, "Google Maps API", total_search_cost,
                description=f"Nearby search within {req.radius_m}m ({len(leads)} leads)"
            )

        return {"leads": leads}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/audit/progress")
async def audit_progress(
    website: str,
    _auth: None = Depends(require_api_key),
    # Polled roughly once a second per in-flight audit, so it needs the same
    # generous ceiling as the other poll endpoints (/api/costs etc), not
    # /api/audit's strict 5/min.
    _rl: None = Depends(rate_limit(300, 60)),
):
    """Current stage of an in-flight audit, or {"running": false} if none."""
    entry = _audit_progress.get(_audit_cache_key(website))
    if not entry:
        return {"running": False}
    _, data = entry
    return {"running": True, **data}


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

        if not req.force:
            cached = _audit_cache_get(req.website)
            if cached is not None:
                # Labelled so the caller can tell a fresh audit from a
                # replayed one — a result with no timestamp and no marker is
                # indistinguishable from a live measurement.
                return {**cached, "cached": True}

    try:
        # Check SES quota (optional but good for safety)
        quota = await asyncio.to_thread(ses.check_quota)
        remaining_quota = quota.get('Max24HourSend', 0) - quota.get('SentLast24Hours', 0)
        if remaining_quota <= 0:
            return {"error": "SES quota exceeded."}

        # 1. Grab Screenshot, HTML, and run Playwright-based audits (axe-core, broken links, perf timing)
        _progress_set(req.website, 0)
        image_path = None
        html_content = None
        extra_audit_data = None
        if req.website:
            image_path, html_content, extra_audit_data = await generate_audit_screenshot(req.website, req.company)

            # Playwright couldn't render the page after every retry — on
            # explicit request, don't draft anything from this. This exact
            # failure mode (a bot-detection challenge or a slow loading
            # screen defeating our headless browser, not the site actually
            # being down) produced multiple real "your website isn't
            # loading"/"this hurts your SEO" emails to sites that were
            # completely fine — see CLAUDE.md §8/§13, 2026-08-07. No amount
            # of careful wording fixes that; the only honest move is not to
            # generate an audit from no real data at all.
            if not html_content:
                return {"error": "Could not access this website after multiple attempts (likely bot-protection or a slow-loading page, not necessarily the site being down) — skipped rather than drafting a guess. Try again in a few minutes, or verify the site manually."}

        # 2. Website Audit (using fully rendered HTML + Playwright audit data)
        _progress_set(req.website, 1)
        web_data = await web_scraper.audit_website(
            req.website,
            html=html_content,
            extra_audit_data=extra_audit_data,
            gbp_phone=req.gbp_phone,
            gbp_address=req.gbp_address,
        )

        # 3. Instagram Data — use handle from request, or auto-detect from website
        _progress_set(req.website, 2)
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
        #
        # mobile_image_path and rating/reviews_count were missing here until
        # 2026-08-09 while main.py's batch runner passed all three — so on
        # this path (the dashboard, i.e. the one actually used) the separate
        # mobile screenshot was captured, paid for in wall-clock time, used
        # for mobile axe-core/overflow flaws, and then never shown to the
        # model, leaving the whole has_mobile_image prompt block dead. Same
        # for the Google Business rating personalization hook.
        _progress_set(req.website, 3)
        analysis = await asyncio.to_thread(
            auditor.analyze_lead,
            req.company, ig_data, web_data,
            image_path=image_path,
            mobile_image_path=(extra_audit_data or {}).get("mobile_image_path"),
            rating=req.rating,
            reviews_count=req.reviews_count,
            gbp_phone=req.gbp_phone,
        )

        image_url = None
        if image_path:
            image_url = f"/screenshots/{os.path.basename(image_path)}"

        if not analysis:
            return {"error": "AI failed to analyze."}

        # 5. Find Contact (using fully rendered HTML)
        _progress_set(req.website, 4)
        dm = await asyncio.to_thread(decision_maker.find_decision_maker, req.company, req.website, html_content=html_content)
        contact = dm.get("name", "")
        email = dm.get("email", "")

        dm_cost = dm.get("cost", 0.0)
        if dm_cost:
            await asyncio.to_thread(db.log_cost, "AI Web Fetch", dm_cost, description=f"Contact discovery for {req.company}")

        # Recipient accuracy is worth more than claim accuracy: a perfectly
        # true email sent to an address nobody owns is a hard bounce, and
        # bounces damage sender reputation for every future send. These ride
        # the existing review_warnings channel so they surface on the draft
        # card AND trip /api/send's acknowledgement gate.
        if dm.get("domain_accepts_mail") is False:
            analysis.setdefault("review_warnings", []).append(
                f"{email} — this domain does not resolve or accept mail at all. Sending here is a guaranteed hard bounce, which damages sender reputation for every future email. Verify the address before sending."
            )
        elif dm.get("is_guess"):
            analysis.setdefault("review_warnings", []).append(
                f"{email} was GUESSED from a common pattern, not found on the site — no lookup confirmed this mailbox exists. Check it before sending; a wrong address bounces, and bounces hurt deliverability for every later email."
            )

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
        _progress_set(req.website, 5)
        await asyncio.to_thread(
            db.log_draft,
            company=req.company,
            website=req.website,
            target_email=email,
            subject=subject,
            body=body,
            image_url=image_url or "",
            review_warnings=analysis.get("review_warnings") or [],
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
            "ai_cost": analysis.get("ai_cost", 0.0001),
            # Which checks actually produced data — lets the UI show that a
            # flaw category was never measured, instead of an absent flaw
            # being indistinguishable from a clean result.
            "signal_status": getattr(web_data, "signal_status", {}) or {},
            # Accuracy safety-net findings (hallucination/grounding/spam-word
            # checks in analyzer/ai_audit.py) — previously only a server log
            # line, now surfaced so a flagged draft is visible before send.
            "review_warnings": analysis.get("review_warnings") or [],
        }
        if req.website:
            _audit_cache_set(req.website, result)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # Always clear, including on the error path — a failed audit must
        # not leave a stale "still running" entry the frontend keeps polling.
        _progress_clear(req.website)

@app.post("/api/send")
async def send_email(
    req: SendRequest,
    background_tasks: BackgroundTasks,
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(10, 60)),
):
    try:
        # Enforce the daily sending cap HERE, because this is the only path
        # emails actually go out on. main.py's batch loop has its own
        # DAILY_EMAIL_LIMIT check, but that loop only ever drafts (its
        # "emailed" branch is unreachable — process_single_lead returns
        # "drafted"), so before this the cap constrained how many leads got
        # drafted and nothing at all about send volume. On a freshly
        # un-sandboxed domain the volume ramp is the entire warm-up
        # strategy, so it needs teeth on the real send path.
        sent_today = await asyncio.to_thread(db.count_emails_sent_today)
        if sent_today >= config.DAILY_EMAIL_LIMIT:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Daily sending limit reached ({sent_today}/{config.DAILY_EMAIL_LIMIT} in the last 24h). "
                    "This cap protects sender reputation while the domain warms up — raise DAILY_EMAIL_LIMIT "
                    "gradually once Postmaster Tools shows spam placement improving."
                ),
            )

        # The five accuracy checks in analyzer/ai_audit.py already flag a
        # draft whose copy looks fabricated, and their output has been
        # visible in the Drafts UI since 2026-08-07 — but nothing stopped a
        # flagged draft being sent anyway, so the whole safety net came down
        # to whether a human happened to read a red banner before clicking.
        # A draft's audit data is also frozen at generation time while the
        # draft itself sits in the inbox indefinitely, so an old draft can
        # cite findings that are no longer true. Both now need one explicit
        # acknowledgement rather than passing silently.
        draft = await asyncio.to_thread(db.get_draft_by_website, req.website)
        if draft and not req.acknowledge_warnings:
            blockers = list(draft.get("review_warnings") or [])

            if config.DRAFT_STALE_DAYS > 0 and draft.get("timestamp"):
                try:
                    drafted_at = datetime.fromisoformat(str(draft["timestamp"]))
                    age_days = (datetime.now() - drafted_at).days
                    if age_days >= config.DRAFT_STALE_DAYS:
                        blockers.append(
                            f"This draft was generated {age_days} days ago — the site may have changed since, "
                            f"so its findings may no longer be accurate. Re-audit to be sure."
                        )
                except (TypeError, ValueError):
                    # An unparseable timestamp shouldn't block a send that is
                    # otherwise fine — the warnings check above still applies.
                    pass

            if blockers:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "This draft was flagged during review and hasn't been acknowledged yet.",
                        "warnings": blockers,
                        "resend_with": "acknowledge_warnings: true",
                    },
                )

        # Use existing screenshot (same collision-safe name generate_audit_screenshot wrote)
        image_path = None
        if not req.attach_screenshot:
            print(f"[Send] Sending to {req.email} without the screenshot — attachment disabled for this draft.")
        elif req.company and req.website:
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
            # Record which copy variant this was, so a reply weeks from now
            # can be attributed to a decision rather than to nothing.
            await asyncio.to_thread(
                db.log_email, req.company, req.website, req.email, config.FROM_EMAIL,
                req.subject, req.body, message_id=message_id,
                variant=config.EMAIL_VARIANT,
            )

            # Remove from drafts since it's sent
            await asyncio.to_thread(db.delete_draft_by_website, req.website)

            return {"status": "success"}
        else:
            raise HTTPException(status_code=400, detail=f"{req.email} is on the unsubscribe/suppression list")
    except HTTPException:
        # HTTPException subclasses Exception, so the generic handler below
        # was swallowing the deliberate 400 above and re-raising it as a
        # 500 — collapsing "recipient is unsubscribed" back into the same
        # opaque 500 the 2026-07-14 fix set out to eliminate, and it would
        # do the same to the 429 daily-cap response. Let intentional status
        # codes through untouched.
        raise
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

# Deliberately unauthenticated and unrate-limited, same reasoning as
# /unsubscribe above: the caller is a recipient's mail client, which has no
# API key and no way to retry a rejection. The tracking ID is a digest of the
# message's Message-ID (emailer/tracking.py), so hits can't be forged for a
# message the sender never sent.
@app.get("/o/{tracking_id}.gif")
async def tracking_pixel(tracking_id: str, request: Request):
    """
    Log one open and return a 1x1 transparent GIF.

    This route must return the image no matter what goes wrong. A 404 or a
    500 here renders as a broken-image icon inside someone's inbox, which is
    both visibly odd and a giveaway that the mail is tracked — so logging
    failures are swallowed rather than surfaced.
    """
    try:
        # Only accept the exact shape tracking_id_for() produces (18 hex
        # chars), so scanners probing this path don't fill the table.
        if len(tracking_id) == 18 and all(c in "0123456789abcdef" for c in tracking_id):
            user_agent = request.headers.get("user-agent", "")
            client_ip = request.client.host if request.client else ""
            await asyncio.to_thread(
                db.log_email_open,
                tracking_id,
                user_agent,
                hash_ip(client_ip),
                looks_automated(user_agent),
            )
    except Exception as e:
        print(f"[Tracking] Could not log open for {tracking_id}: {e}")

    return Response(
        content=TRANSPARENT_GIF,
        media_type="image/gif",
        headers={
            # Without this the client caches the pixel and a second open
            # never reaches us.
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.post("/api/check-replies")
async def check_replies(_auth: None = Depends(require_api_key), _rl: None = Depends(rate_limit(10, 60))):
    """
    Scan the reply mailbox over IMAP and record anything matching a sent email.

    Rate-limited low: each call opens a real IMAP session and walks a rolling
    window of messages, so it's a poll to run occasionally, not on a timer.
    """
    from emailer.reply_checker import check_replies as run_check

    try:
        return await asyncio.to_thread(run_check)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


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
        # The frontend needs this to tell "nobody opened it" apart from
        # "opens were never measured" — both look like open_count 0.
        variant_performance = await asyncio.to_thread(db.get_variant_performance)
        return {
            "history": history,
            "tracking_enabled": bool(config.EMAIL_OPEN_TRACKING and config.APP_BASE_URL),
            "reply_checking_enabled": bool(config.IMAP_HOST and config.IMAP_USER and config.IMAP_PASSWORD),
            # Reply rate per copy variant — the point of recording `variant`
            # at send time. Rows below the minimum-sends bar carry
            # enough_data: false and must not be read as a result yet.
            "variant_performance": variant_performance,
            "current_variant": config.EMAIL_VARIANT,
        }
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
