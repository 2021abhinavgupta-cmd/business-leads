import asyncio
import os
import random
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime
from typing import Literal
from fastapi import FastAPI, HTTPException, BackgroundTasks, Header, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import config
from scrapers.google_maps import GoogleMapsScraper
from scrapers.indiamart_dork import IndiaMartDorkScraper, TradeIndiaDorkScraper, ExportersIndiaDorkScraper
from scrapers.krishi_maharashtra import KrishiMaharashtraScraper
from scrapers.website import WebsiteScraper
from scrapers.instagram import InstagramScraper
from analyzer.ai_audit import AIAuditor
from emailer import get_sender
from emailer.tracking import TRANSPARENT_GIF, hash_ip, looks_automated
from enrichment.decision_maker import DecisionMaker
from analyzer.visuals import (
    generate_audit_screenshot,
    make_screenshot_filename,
    make_mobile_screenshot_filename,
    make_closeup_screenshot_filename,
)
from analyzer.budget_signal import estimate_budget_fit, clears_min_tier
from analyzer.mca_lookup import lookup_company as lookup_mca_company
from analyzer.social_audit import audit_profile, generate_social_outreach
from scrapers.social.discover import find_social_handles
from scrapers.social.instagram import fetch_instagram
from scrapers.social.youtube import fetch_youtube
from scrapers.social.facebook import fetch_facebook
from scrapers.social.linkedin import fetch_linkedin
from dataclasses import asdict as _dataclass_asdict
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


# How recent a draft has to be to count as "produced by the audit that just
# appeared to fail" — see /api/audit/recover below. Reuses _AUDIT_CACHE_TTL
# rather than choosing an independent number: it's already this codebase's
# definition of "still the same audit, don't treat it as new."
_RECOVERABLE_DRAFT_WINDOW_SECONDS = _AUDIT_CACHE_TTL


def _is_recoverable(draft: dict | None) -> bool:
    """
    True if *draft* is fresh enough to be the product of a just-failed
    /api/audit call rather than an unrelated older draft for the same site.

    /api/audit is a single request that runs for a couple of minutes with
    nothing in this codebase that cancels the coroutine if the client's
    connection drops mid-request — Starlette does not do this on its own for
    a plain response, only if the handler explicitly polls
    request.is_disconnected(). A dropped connection (an edge/proxy idle
    timeout, a flaky network) therefore looks like a failed audit to the
    browser while the backend keeps running to completion and saves a real
    draft anyway — live-reported: the UI showed "Audit failed" for a lead
    that had a usable draft sitting in the database the whole time.

    Without this recency check, /api/audit/recover would also hand back a
    genuinely stale, unrelated draft from a much earlier audit whenever the
    CURRENT attempt failed for a real reason (the AI truly erroring, say),
    making a real failure look like a success. sqlite's CURRENT_TIMESTAMP is
    UTC, so this compares against datetime.utcnow() rather than
    datetime.now() — unlike the day-granularity staleness check in
    /api/send, which is forgiving enough that datetime.now()'s local-vs-UTC
    skew doesn't matter, a minute-granularity check needs to get this right.
    """
    if not draft or not draft.get("timestamp"):
        return False
    try:
        drafted_at = datetime.fromisoformat(str(draft["timestamp"]))
    except (TypeError, ValueError):
        return False
    age_seconds = (datetime.utcnow() - drafted_at).total_seconds()
    return age_seconds <= _RECOVERABLE_DRAFT_WINDOW_SECONDS


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
    # When true, /api/search returns almost immediately ({"started": True,
    # "key": ...}) and does the actual scrape in a detached background task
    # instead of blocking the response — same async_mode pattern as
    # AuditRequest.async_mode (see its docstring). Added specifically for the
    # Google Maps free-scraper fallback (scrapers/google_maps.py), which
    # shares a global Playwright semaphore(1) with every in-progress audit
    # and can sit queued behind one for minutes with the button showing
    # nothing but a spinner — live-reported 2026-09-08. Default False so
    # every existing programmatic/test caller of this endpoint is unaffected.
    async_mode: bool = False

class B2BDirectorySearchRequest(BaseModel):
    niche: str
    city: str = ""
    limit: int = Field(default=20, ge=1, le=50)
    # Unlike /api/search-nearby (a genuinely different request/response
    # shape), these three sources share one shape and differ only in which
    # domain gets dorked — a mode flag on one endpoint rather than three
    # near-identical routes. Invalid values 422 via the Literal, so a typo
    # fails loud instead of silently falling back to indiamart.
    directory: Literal["indiamart", "tradeindia", "exportersindia"] = "indiamart"

class KrishiMaharashtraSearchRequest(BaseModel):
    # No niche — this source is one fixed government dataset (licensed seed
    # dealers), not a search. City narrows by district/taluka.
    city: str = ""
    limit: int = Field(default=50, ge=1, le=200)

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
    # Set by the frontend when the lead came from a sector-specific section
    # (e.g. the Agriculture tab). Empty for every normal search, so this only
    # changes copy for leads explicitly tagged — see BaseSender.generate_email.
    sector: str = ""
    # The niche/category clicked (e.g. "Irrigation Equipment Supplier") or,
    # for Krishi Maharashtra leads, the license category (e.g. "Fertilizer
    # Dealer"). Lets the sector line name a specific real scheme (PM-KUSUM,
    # SMAM) instead of one generic sentence for every agriculture niche.
    sector_detail: str = ""
    # Set only by the dashboard's dedicated "Generate for Agriculture"
    # button (agriculture-sourced leads only) — adds the Metazyne/
    # @agriusindia credibility line to the drafted email. Deliberately not
    # automatic for every sector=="agriculture" lead: the ordinary "Generate
    # AI Audit & Draft" button never sets this. See
    # BaseSender.generate_email's include_agri_credibility docstring.
    include_agri_credibility: bool = False
    # Same mechanism as include_agri_credibility above, but for the
    # dedicated "Generate for Textile" button (sector=="textile" leads
    # only) — adds the Alpine Texworld credibility line. Added 2026-09-08.
    include_textile_credibility: bool = False
    # When true, /api/audit returns almost immediately ({"started": True})
    # and does the actual work in a background task instead of blocking the
    # HTTP response for the couple of minutes a real audit takes. Exists
    # because Railway's edge/proxy times out a request well before that,
    # 502ing every single audit regardless of whether the backend itself is
    # fine — live-confirmed via the deploy log (audit still cleanly
    # progressing at 65s+ in, no crash, no restart, nothing in the backend
    # log at all, so the response was cut off in front of the app, not by
    # it). Default False so every existing programmatic/test caller of this
    # endpoint (main.py, the whole test suite) is completely unaffected —
    # only the dashboard frontend sets this. See /api/audit/result below.
    async_mode: bool = False

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
b2b_directory_scrapers = {
    "indiamart": IndiaMartDorkScraper(),
    "tradeindia": TradeIndiaDorkScraper(),
    "exportersindia": ExportersIndiaDorkScraper(),
}
krishi_maharashtra_scraper = KrishiMaharashtraScraper()
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

# Live progress + async-mode result handoff for /api/search, mirroring
# _audit_progress/_audit_async_results above (see AuditRequest.async_mode's
# docstring for the general pattern). Keyed by a random token minted per
# search request rather than by website/niche — unlike an audit, a search
# has no natural stable key, and two identical searches in flight at once
# are a real, unremarkable case (Autopilot + a manual search, say).
_SEARCH_STAGE_LABELS = {
    "searching_api": "Searching Google Places API",
    "falling_back_to_scraper": "Places API returned nothing usable — falling back to the free scraper. "
                                "This can take a few minutes and waits for any audits already running.",
}
_SEARCH_PROGRESS_TTL = 900
_search_progress: dict[str, tuple[float, dict]] = {}
_SEARCH_ASYNC_RESULT_TTL = 900
_search_async_results: dict[str, tuple[float, dict]] = {}


def _search_progress_set(key: str, stage_name: str) -> None:
    now = time.monotonic()
    for k, (ts, _) in list(_search_progress.items()):
        if now - ts > _SEARCH_PROGRESS_TTL:
            _search_progress.pop(k, None)
    _search_progress[key] = (now, {"stage": _SEARCH_STAGE_LABELS.get(stage_name, stage_name)})


async def _search_leads_impl(req: SearchRequest, background_tasks: BackgroundTasks, progress_key: str | None = None) -> dict:
    try:
        on_stage = (lambda name: _search_progress_set(progress_key, name)) if progress_key else None
        leads = await maps_scraper.scrape_google_maps(req.niche, req.city, limit=req.limit, on_stage=on_stage)
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


@app.post("/api/search")
async def search_leads(
    req: SearchRequest,
    background_tasks: BackgroundTasks,
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(5, 60)),
):
    if req.async_mode:
        # Fire the real work as a detached task and return immediately —
        # same reasoning as AuditRequest.async_mode. A fresh,
        # request-independent BackgroundTasks() is used since the one
        # FastAPI injected here belongs to this request's lifecycle, which
        # is about to end.
        key = uuid.uuid4().hex

        async def _run_and_store():
            local_background_tasks = BackgroundTasks()
            try:
                result = await _search_leads_impl(req, local_background_tasks, progress_key=key)
            except HTTPException as e:
                result = {"error": e.detail}
            except Exception as e:
                result = {"error": str(e)}
            await local_background_tasks()
            now = time.monotonic()
            for k, (ts, _) in list(_search_async_results.items()):
                if now - ts > _SEARCH_ASYNC_RESULT_TTL:
                    _search_async_results.pop(k, None)
            _search_async_results[key] = (now, result)
            _search_progress.pop(key, None)

        asyncio.create_task(_run_and_store())
        return {"started": True, "key": key}

    return await _search_leads_impl(req, background_tasks)


@app.get("/api/search/progress")
async def search_progress(
    key: str,
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(300, 60)),
):
    """Current stage of an in-flight async_mode /api/search call, keyed by
    the token that call's {"started": True, "key": ...} response returned."""
    entry = _search_progress.get(key)
    if not entry:
        return {"running": False}
    _, data = entry
    return {"running": True, **data}


@app.get("/api/search/result")
async def search_result(
    key: str,
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(300, 60)),
):
    """
    Poll target for an async_mode /api/search call. {"ready": false} until
    the background task finishes, then the stored result (popped — one-shot
    handoff per search, not a replay cache).
    """
    entry = _search_async_results.pop(key, None)
    if not entry:
        return {"ready": False}
    _, result = entry
    return {"ready": True, **result}

@app.post("/api/search-b2b-directory")
async def search_leads_b2b_directory(
    req: B2BDirectorySearchRequest,
    background_tasks: BackgroundTasks,
    _auth: None = Depends(require_api_key),
    # DuckDuckGo dorking, not a paid API — rate-limited to stay polite to DDG
    # rather than to control cost. Same limit as /api/search for consistency.
    _rl: None = Depends(rate_limit(5, 60)),
):
    """
    Find suppliers/dealers listed on IndiaMART/TradeIndia/ExportersIndia for
    a niche, free (see scrapers/indiamart_dork.py's B2BDirectoryDorkScraper).

    Separate endpoint from /api/search rather than folded into it: this is a
    DuckDuckGo site: dork against a directory (see StartupDorkScraper for the
    same pattern), not the Google Maps Places API, and returns leads that
    often have no Website (blank, not the directory listing URL — auditing
    that would measure the directory, not the lead) alongside ones that do.
    All three directories share this one route via `directory`, unlike
    /api/search-nearby which has a genuinely different request/response shape.
    """
    try:
        scraper = b2b_directory_scrapers[req.directory]
        leads = await asyncio.to_thread(scraper.scrape, req.niche, req.city, req.limit)
        background_tasks.add_task(save_leads_to_sheets_bg, leads)
        return {"leads": leads}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/search-krishi-maharashtra")
async def search_leads_krishi_maharashtra(
    req: KrishiMaharashtraSearchRequest,
    background_tasks: BackgroundTasks,
    _auth: None = Depends(require_api_key),
    # Parsed from a cached local PDF, not a live external call per request —
    # generous limit, matching /api/costs' polling-tier rather than the
    # scraper-tier 5/min.
    _rl: None = Depends(rate_limit(20, 60)),
):
    """
    Return licensed seed-dealer firms from Maharashtra's own government
    publication (see scrapers/krishi_maharashtra.py). Real government data,
    not a scrape of a third party — separate endpoint since it takes no
    niche at all (one fixed dataset) and needs no rate-limit protection for
    an external service the way the dork/Maps endpoints do.
    """
    try:
        leads = await asyncio.to_thread(krishi_maharashtra_scraper.scrape, req.city, req.limit)
        background_tasks.add_task(save_leads_to_sheets_bg, leads)
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


# Holds the final result of an async_mode audit until the frontend picks it
# up via /api/audit/result. Same in-memory/single-instance tradeoff as
# _audit_cache/_audit_progress above. Popped on read (a one-shot handoff,
# not a cache — _audit_cache already covers "replay the last result").
_AUDIT_ASYNC_RESULT_TTL = 900
_audit_async_results: dict[str, tuple[float, dict]] = {}

# The live asyncio.Task behind each in-flight async_mode audit, keyed the
# same way as _audit_progress/_audit_async_results — lets /api/audit/cancel
# find and cancel one. Entries remove themselves once the task finishes
# (success, error, or cancellation), so this never grows unbounded.
_audit_tasks: dict[str, asyncio.Task] = {}


@app.post("/api/audit")
async def audit_lead(
    req: AuditRequest,
    background_tasks: BackgroundTasks,
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(5, 60)),
):
    if req.async_mode and req.website:
        # Fire the real work as a detached task and return immediately —
        # see AuditRequest.async_mode's docstring for why. A fresh,
        # request-independent BackgroundTasks() is used (not the one FastAPI
        # injected here) because that one's lifecycle is tied to THIS
        # request/response, which is about to finish; anything added to it
        # after the response is sent would never run.
        key = _audit_cache_key(req.website)

        # If an earlier async_mode audit for this exact website is still
        # in flight, don't silently orphan it under _audit_tasks[key] —
        # that dict holds only one task per key, so the line below
        # (_audit_tasks[key] = task) would otherwise overwrite the
        # reference to the still-running old task with no way left to
        # reach it. Reported live as "even after cancelling [an audit]
        # it's still running": /api/audit/cancel can only ever cancel
        # whichever task is CURRENTLY in _audit_tasks[key], so a second
        # /api/audit call for the same website (a double-click, a retry
        # fired while the first was still going, Autopilot racing a
        # manual click) could leave the real first attempt uncancellable
        # and free to finish — including saving a real draft — well
        # after the user believed "the" audit for this lead was stopped.
        existing_task = _audit_tasks.get(key)
        if existing_task and not existing_task.done():
            existing_task.cancel()

        async def _run_and_store():
            local_background_tasks = BackgroundTasks()
            try:
                result = await _audit_lead_impl(req, local_background_tasks)
            except asyncio.CancelledError:
                # Cancelled via /api/audit/cancel — _audit_lead_impl's own
                # `finally` already cleared _audit_progress for this key, so
                # the frontend's poll loop is already seeing "not running";
                # this just gives it a clean result to stop on instead of
                # polling /api/audit/result forever with nothing ever
                # written. Deliberately swallowed, not re-raised: the task
                # is done either way, and we still want the bookkeeping
                # below (TTL sweep, storing the result) to run.
                result = {"cancelled": True}
            except HTTPException as e:
                result = {"error": e.detail}
            except Exception as e:
                result = {"error": str(e)}
            await local_background_tasks()
            now = time.monotonic()
            for k, (ts, _) in list(_audit_async_results.items()):
                if now - ts > _AUDIT_ASYNC_RESULT_TTL:
                    _audit_async_results.pop(k, None)
            _audit_async_results[key] = (now, result)

        task = asyncio.create_task(_run_and_store())
        _audit_tasks[key] = task
        task.add_done_callback(lambda t, k=key: _audit_tasks.pop(k, None))
        return {"started": True}

    return await _audit_lead_impl(req, background_tasks)


@app.get("/api/audit/result")
async def audit_result(
    website: str,
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(300, 60)),
):
    """
    Poll target for an async_mode /api/audit call. {"ready": false} until
    the background task finishes, then the stored result (popped — one-shot
    handoff per audit attempt, not a replay cache).
    """
    entry = _audit_async_results.pop(_audit_cache_key(website), None)
    if not entry:
        return {"ready": False}
    _, result = entry
    return {"ready": True, **result}


@app.post("/api/audit/cancel")
async def cancel_audit(
    website: str,
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(30, 60)),
):
    """
    Cancel an in-flight async_mode audit for *website*, requested as
    "add an option of cancelling the ongoing audit". task.cancel() raises
    CancelledError at whatever await point the audit is currently sitting
    at; _audit_lead_impl's own `finally` clears _audit_progress either way,
    and _run_and_store's CancelledError handler stores a clean
    {"cancelled": True} result so the frontend's existing poll loop resolves
    instead of waiting on a result that would otherwise never arrive.

    Note: if the audit is mid-way through a Playwright call
    (asyncio.to_thread), that one call keeps running in its worker thread to
    completion — cancellation stops the app from waiting on/using it, it
    doesn't kill the thread. Same limitation asyncio.to_thread cancellation
    always has; not worth working around for a "never mind, stop" button.
    """
    task = _audit_tasks.get(_audit_cache_key(website))
    if not task or task.done():
        return {"cancelled": False}
    task.cancel()
    return {"cancelled": True}


async def _audit_lead_impl(req: AuditRequest, background_tasks: BackgroundTasks) -> dict:
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
            print("[Audit] WARNING: SES quota exceeded. The draft will be generated but cannot be sent until quota resets.")

        # 1. Grab Screenshot, HTML, and run Playwright-based audits (axe-core, broken links, perf timing)
        _progress_set(req.website, 0)
        image_path = None
        html_content = None
        extra_audit_data = None
        if req.website:
            # generate_audit_screenshot fires on_queued the instant it sees the
            # global Playwright semaphore (analyzer/visuals.py) already held by
            # another audit/search, and on_started once it actually acquires
            # it — so "Loading site & capturing screenshots" only ever means
            # real work is happening, and a genuinely queued lead says so
            # instead of looking identically, silently stuck.
            image_path, html_content, extra_audit_data = await generate_audit_screenshot(
                req.website,
                req.company,
                on_queued=lambda: _progress_set(
                    req.website, 0,
                    note="Queued — waiting for another audit/search to finish (only one can run at a time)",
                ),
                on_started=lambda: _progress_set(req.website, 0),
            )

            # Playwright couldn't render the page after every retry.
            # Originally this returned an error, but we are bypassing this block
            # to let the audit fall back to the httpx strategy downstream.
            if not html_content:
                print("[Audit] WARNING: Could not access website via Playwright. Falling back to httpx without screenshot/HTML.")

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

        # 3b. Established-business gate. budget_signal is computed here (rather
        # than only at the end for the dashboard badge) so a lead that shows
        # no sign of being an established enough business is skipped BEFORE
        # the AI draft and the send — every free scale signal is in hand by
        # now (reviews, detected tooling, booking widget, IG reach, MCA), and
        # the audit has already run so "unclear" means "checked, found
        # nothing", not "haven't looked". No-op when MIN_BUDGET_TIER is empty.
        budget_signal = estimate_budget_fit(
            rating=req.rating,
            reviews_count=req.reviews_count,
            technologies=getattr(web_data, "technologies", None),
            has_booking_widget=getattr(web_data, "has_booking_widget", False),
            ig_followers=getattr(ig_data, "followers", None) if ig_data else None,
            # A no-op (returns None immediately) until DATA_GOV_IN_API_KEY and
            # MCA_COMPANY_MASTER_RESOURCE_ID are both set.
            mca_match=await asyncio.to_thread(lookup_mca_company, req.company),
        )
        if not clears_min_tier(budget_signal, config.MIN_BUDGET_TIER):
            return {
                "error": (
                    f"Skipped: {req.company} doesn't show enough signal of being an "
                    f"established business to be worth an audit (budget tier "
                    f"'{budget_signal['tier']}', minimum is '{config.MIN_BUDGET_TIER}'). "
                    f"Lower or clear MIN_BUDGET_TIER to change this."
                ),
                "skipped_reason": "below_min_budget_tier",
                "budget_signal": budget_signal,
            }

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
        subject, body = ses.generate_email(
            req.company, contact, analysis, YOUR_NAME,
            sector=req.sector, sector_detail=req.sector_detail,
            include_agri_credibility=req.include_agri_credibility,
            include_textile_credibility=req.include_textile_credibility,
        )

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
            # Prioritisation only, for the operator's own dashboard — never
            # read by ai_audit.py or base_sender.py, so it structurally
            # cannot leak into the drafted copy. See analyzer/budget_signal.py.
            # Computed once above (it also gates the audit), reused here.
            "budget_signal": budget_signal,
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

@app.get("/api/audit/recover")
async def recover_audit(
    website: str,
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(30, 60)),
):
    """
    Was a draft actually produced for *website* despite /api/audit appearing
    to fail? See _is_recoverable's docstring for why this can happen — the
    short version is that a dropped connection mid-request looks identical
    to a real failure to the browser, but the backend has already finished
    and paid for the whole pipeline by the time that happens.

    The frontend calls this from handleAudit's catch block before settling
    on 'failed', so a client-side network hiccup doesn't throw away real,
    already-completed work or send the human on a wasted, cost-incurring
    retry of an audit that in fact succeeded.
    """
    try:
        draft = await asyncio.to_thread(db.get_draft_by_website, website)
        if not _is_recoverable(draft):
            return {"draft": None}
        return {"draft": draft}
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

        # Use existing screenshots (same collision-safe names
        # generate_audit_screenshot wrote). All three captures are attached
        # now: the desktop one carries the marked evidence, the close up is a
        # tight crop of that same marked element, and the mobile one is what
        # most of these prospects' own customers actually see. Each is
        # captioned for what it really is in base_sender — the single
        # hardcoded "on mobile" caption used to sit above the DESKTOP image on
        # every send.
        image_path = None
        mobile_image_path = None
        closeup_image_path = None
        if not req.attach_screenshot:
            print(f"[Send] Sending to {req.email} without the screenshot — attachment disabled for this draft.")
        elif req.company and req.website:
            for filename, target in (
                (make_screenshot_filename(req.company, req.website), "desktop"),
                (make_mobile_screenshot_filename(req.company, req.website), "mobile"),
                (make_closeup_screenshot_filename(req.company, req.website), "closeup"),
            ):
                candidate_path = os.path.join(SCREENSHOTS_DIR, filename)
                if os.path.exists(candidate_path):
                    if target == "desktop":
                        image_path = candidate_path
                    elif target == "mobile":
                        mobile_image_path = candidate_path
                    else:
                        closeup_image_path = candidate_path

        message_id = await asyncio.to_thread(
            ses.send_email, req.email, req.subject, req.body,
            image_path=image_path, mobile_image_path=mobile_image_path,
            closeup_image_path=closeup_image_path,
        )
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

class GenerateFollowupRequest(BaseModel):
    history_id: int
    stage: int = 1

@app.post("/api/generate-followup")
async def generate_followup(
    req: GenerateFollowupRequest,
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(10, 60)),
):
    """
    History tab's "Generate Follow-up" button. Reads one specific past
    send (by its email_history row id, not by website — a lead can have
    more than one send over time) and drafts a follow-up that references
    what that exact email actually said, via AIAuditor.generate_followup_
    from_original — a real AI call, unlike scheduler.py's automated
    sequence which uses BaseSender.generate_followup's hardcoded, name-
    and-stage-only copy. Returns a draft for the frontend to show inline
    and edit before sending; nothing is sent or persisted here.
    """
    original = await asyncio.to_thread(db.get_email_history_by_id, req.history_id)
    if not original:
        raise HTTPException(status_code=404, detail="No such sent email.")

    result = await asyncio.to_thread(
        auditor.generate_followup_from_original,
        original.get("company", ""),
        # No decision-maker name is stored on the history row — the
        # original recipient's display name was never persisted, only
        # the address. "there" is the same safe fallback generate_email
        # itself uses for a name it can't confirm is a real person.
        "there",
        os.getenv("YOUR_NAME", "Kshitij Gupta"),
        original.get("subject", ""),
        original.get("body", ""),
        stage=req.stage,
    )
    if result is None:
        raise HTTPException(status_code=502, detail="All AI providers failed to draft a follow-up. Try again shortly.")

    ai_cost = result.get("ai_cost", 0.0)
    if ai_cost:
        await asyncio.to_thread(db.log_cost, "AI Followup", ai_cost, description=f"Follow-up draft for {original.get('company', '')}")

    return {
        "subject": result["subject"],
        "body": result["body"],
        "to_email": original.get("target_email", ""),
        "company": original.get("company", ""),
        "website": original.get("website", ""),
        "original_message_id": original.get("message_id", ""),
    }


class SendFollowupRequest(BaseModel):
    history_id: int
    subject: str
    body: str

@app.post("/api/send-followup")
async def send_followup_route(
    req: SendFollowupRequest,
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(10, 60)),
):
    """
    Sends the (possibly hand-edited) draft /api/generate-followup produced.
    Re-derives the recipient/company/website/original message id from the
    history row itself rather than trusting them from the client — only
    the subject/body a human may have edited come from the request body.
    """
    sent_today = await asyncio.to_thread(db.count_emails_sent_today)
    if sent_today >= config.DAILY_EMAIL_LIMIT:
        raise HTTPException(status_code=429, detail=f"Daily sending limit reached ({sent_today}/{config.DAILY_EMAIL_LIMIT} in the last 24h).")

    original = await asyncio.to_thread(db.get_email_history_by_id, req.history_id)
    if not original:
        raise HTTPException(status_code=404, detail="No such sent email.")

    to_email = original.get("target_email", "")
    if not to_email:
        raise HTTPException(status_code=400, detail="That sent email has no recipient address on record.")

    try:
        success = await asyncio.to_thread(
            ses.send_followup, to_email, req.subject, req.body,
            in_reply_to=original.get("message_id", "") or "",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not success:
        raise HTTPException(status_code=400, detail="Send failed — recipient may be on the suppression list, or the transport rejected it. Check server logs.")

    await asyncio.to_thread(db.log_cost, "AWS SES", 0.0001, description=f"Follow-up to {to_email}")
    # message_id left blank: send_followup builds its own internally but
    # does not return it (its only other caller, main.py's batch runner,
    # has never logged follow-up sends to email_history at all — this is
    # additive, not a regression). A second follow-up generated from this
    # row later simply will not have anything to thread against.
    await asyncio.to_thread(
        db.log_email, original.get("company", ""), original.get("website", ""),
        to_email, config.FROM_EMAIL, req.subject, req.body,
        variant="followup-ai",
    )
    return {"status": "success"}


@app.get("/api/sent-websites")
async def get_sent_websites(_auth: None = Depends(require_api_key), _rl: None = Depends(rate_limit(120, 60))):
    """
    Every website ever actually emailed, keyed by a loose normalisation
    (scheme/"www."/trailing-slash stripped) of the site — so the leads
    grid can show "Already sent" on a lead re-scraped in a later search,
    which is otherwise a brand-new object with no way to know it was
    already contacted. See db.get_sent_websites_summary().
    """
    try:
        summary = await asyncio.to_thread(db.get_sent_websites_summary)
        return summary
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/drafted-websites")
async def get_drafted_websites(_auth: None = Depends(require_api_key), _rl: None = Depends(rate_limit(120, 60))):
    """
    Every website with a draft currently sitting in email_drafts, keyed the
    same way as /api/sent-websites — see db.get_drafted_websites_summary().
    """
    try:
        summary = await asyncio.to_thread(db.get_drafted_websites_summary)
        return summary
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


# ===========================================================================
# Social Media page (2026-09-10) — see docs/superpowers/specs/2026-09-10-
# social-media-page-design.md. A standalone flow parallel to /api/audit:
# find businesses by niche/city (reusing the Maps scraper), discover their
# social handles off the homepage, audit each platform's presence, and
# generate email / assisted-DM / WhatsApp outreach. Its own social_drafts
# table; DM is never sent automatically.
# ===========================================================================

_SOCIAL_ADAPTERS = {
    "instagram": fetch_instagram,
    "youtube": fetch_youtube,
    "facebook": fetch_facebook,
    "linkedin": fetch_linkedin,
}

_SOCIAL_TTL = 900
_social_search_progress: dict[str, tuple[float, dict]] = {}
_social_search_results: dict[str, tuple[float, dict]] = {}
_social_audit_progress: dict[str, tuple[float, dict]] = {}
_social_audit_results: dict[str, tuple[float, dict]] = {}


class SocialDraftIn(BaseModel):
    company: str
    platform: str
    handle: str
    profile_url: str = ""
    channel: str            # "email" | "dm" | "whatsapp"
    target: str
    subject: str = ""
    body: str
    issues: list = []


class SocialSendIn(BaseModel):
    company: str
    target: str
    subject: str
    body: str


class SocialSearchIn(BaseModel):
    niche: str
    city: str
    limit: int = 10
    async_mode: bool = False


class SocialAuditIn(BaseModel):
    company: str
    handles: dict            # {"instagram": url, "youtube": url, ...}
    async_mode: bool = False


def _social_progress_set(store: dict, key: str, stage: str) -> None:
    now = time.monotonic()
    for k, (ts, _) in list(store.items()):
        if now - ts > _SOCIAL_TTL:
            store.pop(k, None)
    store[key] = (now, {"stage": stage})


def _social_store_result(store: dict, key: str, result: dict) -> None:
    now = time.monotonic()
    for k, (ts, _) in list(store.items()):
        if now - ts > _SOCIAL_TTL:
            store.pop(k, None)
    store[key] = (now, result)


@app.get("/api/social/drafts")
async def social_drafts_list(
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(300, 60)),
):
    drafts = await asyncio.to_thread(db.get_social_drafts)
    return {"drafts": drafts}


@app.post("/api/social/drafts")
async def social_drafts_create(
    req: SocialDraftIn,
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(60, 60)),
):
    row_id = await asyncio.to_thread(
        db.log_social_draft,
        req.company, req.platform, req.handle, req.profile_url,
        req.channel, req.target, req.subject, req.body, req.issues,
    )
    return {"id": row_id}


@app.delete("/api/social/drafts/{draft_id}")
async def social_drafts_delete(
    draft_id: int,
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(60, 60)),
):
    await asyncio.to_thread(db.delete_social_draft, draft_id)
    return {"deleted": True}


@app.post("/api/social/send")
async def social_send(
    req: SocialSendIn,
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(10, 60)),
):
    """Real send for a social EMAIL draft only. Thin wrapper over the
    configured sender: no screenshot, no review_warnings gate, no staleness
    check (that machinery is website-draft specific)."""
    try:
        sender = get_sender()
        message_id = await asyncio.to_thread(
            sender.send_email, req.target, req.subject, req.body
        )
        if not message_id:
            return {"sent": False, "error": "sender returned no message id"}
        await asyncio.to_thread(
            db.log_cost, "Social Outreach", 0.0001,
            description=f"Social email to {req.company}",
        )
        return {"sent": True}
    except Exception as e:  # noqa: BLE001
        return {"sent": False, "error": str(e)}


async def _social_search_impl(req: SocialSearchIn, progress_key: str | None = None) -> dict:
    def _stage(s):
        if progress_key:
            _social_progress_set(_social_search_progress, progress_key, s)

    _stage("Searching Google Maps")
    leads = await maps_scraper.scrape_google_maps(req.niche, req.city, limit=req.limit)
    businesses = []
    for i, lead in enumerate(leads):
        website = lead.get("Website") or ""
        _stage(f"Finding social handles ({i + 1}/{len(leads)})")
        handles = await find_social_handles(website) if website else {
            "instagram": "", "youtube": "", "facebook": "", "linkedin": "",
        }
        businesses.append({
            "company": lead.get("Company", ""),
            "website": website,
            "phone": lead.get("Phone", ""),
            "handles": handles,
        })
    return {"businesses": businesses}


@app.post("/api/social/search")
async def social_search(
    req: SocialSearchIn,
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(5, 60)),
):
    if req.async_mode:
        key = uuid.uuid4().hex

        async def _run():
            try:
                result = await _social_search_impl(req, progress_key=key)
            except Exception as e:  # noqa: BLE001
                result = {"error": str(e)}
            _social_store_result(_social_search_results, key, result)
            _social_search_progress.pop(key, None)

        asyncio.create_task(_run())
        return {"started": True, "key": key}

    return await _social_search_impl(req)


@app.get("/api/social/search/progress")
async def social_search_progress(
    key: str,
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(300, 60)),
):
    entry = _social_search_progress.get(key)
    if not entry:
        return {"running": False}
    _, data = entry
    return {"running": True, **data}


@app.get("/api/social/search/result")
async def social_search_result(
    key: str,
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(300, 60)),
):
    entry = _social_search_results.pop(key, None)
    if not entry:
        return {"ready": False}
    _, result = entry
    return {"ready": True, **result}


def _handle_from_url(platform: str, url: str) -> str:
    """Best-effort handle/slug out of a profile URL for the adapter."""
    u = (url or "").rstrip("/")
    if not u:
        return ""
    return u.split("/")[-1].lstrip("@")


async def _social_audit_impl(req: SocialAuditIn, progress_key: str | None = None) -> dict:
    results = {}
    for platform, url in (req.handles or {}).items():
        if not url or platform not in _SOCIAL_ADAPTERS:
            continue
        if progress_key:
            _social_progress_set(_social_audit_progress, progress_key, f"Analyzing {platform}")
        handle = _handle_from_url(platform, url)
        adapter = _SOCIAL_ADAPTERS[platform]
        try:
            profile = await asyncio.to_thread(adapter, handle)
        except Exception as e:  # noqa: BLE001
            print(f"[SocialAudit] {platform} adapter raised: {e}")
            profile = None

        if profile is None:
            results[platform] = {"profile": None, "issues": [], "outreach": {}, "note": "could not analyze this profile"}
            continue
        if not getattr(profile, "analyzed", True):
            results[platform] = {
                "profile": _dataclass_asdict(profile), "issues": [], "outreach": {},
                "note": profile.note or "could not analyze this profile",
            }
            continue

        issues = audit_profile(profile)
        issue_dicts = [_dataclass_asdict(i) for i in issues]
        outreach = {}
        total_cost = 0.0
        for channel in ("email", "dm", "whatsapp"):
            if progress_key:
                _social_progress_set(_social_audit_progress, progress_key, f"Writing {platform} {channel}")
            copy = await asyncio.to_thread(
                generate_social_outreach, req.company, profile, issues, channel
            )
            outreach[channel] = {"subject": copy["subject"], "body": copy["body"]}
            total_cost += copy.get("cost", 0.0)
        if total_cost > 0:
            await asyncio.to_thread(
                db.log_cost, "Social Outreach", total_cost,
                description=f"Social outreach for {req.company} ({platform})",
            )
        results[platform] = {"profile": _dataclass_asdict(profile), "issues": issue_dicts, "outreach": outreach}

    return {"results": results}


@app.post("/api/social/audit")
async def social_audit(
    req: SocialAuditIn,
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(10, 60)),
):
    if req.async_mode:
        key = uuid.uuid4().hex

        async def _run():
            try:
                result = await _social_audit_impl(req, progress_key=key)
            except Exception as e:  # noqa: BLE001
                result = {"error": str(e)}
            _social_store_result(_social_audit_results, key, result)
            _social_audit_progress.pop(key, None)

        asyncio.create_task(_run())
        return {"started": True, "key": key}

    return await _social_audit_impl(req)


@app.get("/api/social/audit/progress")
async def social_audit_progress(
    key: str,
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(300, 60)),
):
    entry = _social_audit_progress.get(key)
    if not entry:
        return {"running": False}
    _, data = entry
    return {"running": True, **data}


@app.get("/api/social/audit/result")
async def social_audit_result(
    key: str,
    _auth: None = Depends(require_api_key),
    _rl: None = Depends(rate_limit(300, 60)),
):
    entry = _social_audit_results.pop(key, None)
    if not entry:
        return {"ready": False}
    _, result = entry
    return {"ready": True, **result}


# Mount screenshots folder
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
app.mount("/screenshots", StaticFiles(directory=SCREENSHOTS_DIR), name="screenshots")

# Mount Vite frontend (for production deployment on Railway)
frontend_dist = os.path.join(os.path.dirname(__file__), "frontend", "dist")
if os.path.exists(frontend_dist):
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
