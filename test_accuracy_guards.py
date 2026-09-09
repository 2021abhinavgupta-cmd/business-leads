"""
Regression tests for the accuracy guards added after a project-wide audit.

Every test here corresponds to a bug that actually shipped and stayed
invisible for a while, because each one degraded silently into a confident,
wrong result rather than an error. They exist so the same class of failure
can't come back unnoticed.

Unit tests only — no network, no browser, no API keys.
"""

from datetime import datetime, timedelta

import pytest

from analyzer.flaws import Flaw, compute_score
from analyzer.ai_audit import AIAuditor
from scrapers.website import WebsiteScraper


# ---------------------------------------------------------------------------
# Score inflation from failed signals
#
# compute_score subtracts per detected flaw, so fewer flaws = higher score.
# A partially-failed audit therefore produces a HEALTHIER-looking score for
# a site nobody could actually measure, and main.py's ">70 means skip" rule
# silently discarded those leads.
# ---------------------------------------------------------------------------

def _full_flaw_set() -> list[Flaw]:
    return [
        Flaw("performance", "critical", "LCP too slow"),
        Flaw("performance", "high", "low perf score"),
        Flaw("seo", "medium", "low seo score"),
        Flaw("accessibility", "high", "axe violations"),
        Flaw("tech", "medium", "html validity errors"),
    ]


def test_failed_signals_inflate_score_above_skip_threshold():
    """Documents the trap: this is WHY partial coverage must bypass the skip."""
    full = _full_flaw_set()
    # Same site, but Lighthouse / html-validate produced nothing.
    degraded = [f for f in full if f.category not in ("performance", "tech")]

    assert compute_score(full) < 70, "a genuinely bad site should score below the skip threshold"
    assert compute_score(degraded) > 70, (
        "with signals missing the same site scores as 'too good' — the exact "
        "condition that made real leads get discarded"
    )


def test_should_contact_ignores_score_when_coverage_is_partial():
    auditor = AIAuditor.__new__(AIAuditor)  # no API clients needed
    healthy_looking = {"overall_score": 88, "partial_coverage": ["lighthouse", "pa11y"]}
    assert auditor.should_contact(healthy_looking) is True


def test_should_contact_still_skips_genuinely_healthy_sites():
    auditor = AIAuditor.__new__(AIAuditor)
    assert auditor.should_contact({"overall_score": 88, "partial_coverage": []}) is False
    assert auditor.should_contact({"overall_score": 42, "partial_coverage": []}) is True


# ---------------------------------------------------------------------------
# Per-category score capping
# ---------------------------------------------------------------------------

def test_one_noisy_category_cannot_bottom_out_the_score():
    # axe-core routinely reports 15+ violations on an ordinary site.
    noisy = [Flaw("accessibility", "high", f"violation {i}") for i in range(15)]
    score = compute_score(noisy)
    assert score > 0, "a single noisy signal must not flatten the score to zero"
    assert score == 65, "accessibility should contribute exactly the per-category cap (35)"


def test_problems_spread_across_categories_score_worse_than_one_bad_area():
    concentrated = [Flaw("accessibility", "high", f"v{i}") for i in range(6)]
    spread = [
        Flaw("accessibility", "high", "a"),
        Flaw("performance", "high", "b"),
        Flaw("seo", "high", "c"),
        Flaw("security", "high", "d"),
        Flaw("conversion", "high", "e"),
        Flaw("tech", "high", "f"),
    ]
    assert compute_score(spread) < compute_score(concentrated), (
        "breadth of problems should matter more than volume within one area"
    )


def test_clean_site_scores_100():
    assert compute_score([]) == 100


# ---------------------------------------------------------------------------
# Phantom "0/100" — a failed measurement must never read as a real score
# ---------------------------------------------------------------------------

class _StubWeb:
    """Minimal stand-in for WebsiteData; _build_prompt only reads attributes."""

    def __init__(self, page_speed_score=0, seo_score=0, signal_status=None):
        self.page_speed_score = page_speed_score
        self.seo_score = seo_score
        self.signal_status = signal_status or {}
        self.technologies = []
        self.homepage_text = "Some homepage text"
        self.perf_timing = {}
        self.flaws = []
        self.company_context = ""
        self.visual_flaw_context = ""


def test_unmeasured_scores_are_not_presented_as_zero():
    prompt = AIAuditor._build_prompt("Acme", None, _StubWeb(page_speed_score=0, seo_score=0))
    assert "0/100" not in prompt, "a failed measurement must never look like a real score of zero"
    assert "COULD NOT BE MEASURED" in prompt


def test_real_scores_are_still_quoted_normally():
    prompt = AIAuditor._build_prompt("Acme", None, _StubWeb(page_speed_score=42, seo_score=70))
    assert "42/100" in prompt
    assert "70/100" in prompt
    assert "COULD NOT BE MEASURED" not in prompt


# ---------------------------------------------------------------------------
# Coverage disclosure in the prompt
# ---------------------------------------------------------------------------

def test_prompt_discloses_checks_that_returned_no_data():
    web = _StubWeb(
        page_speed_score=55,
        seo_score=60,
        signal_status={"lighthouse": "ok", "pa11y": "no_data", "html_validate": "no_data"},
    )
    prompt = AIAuditor._build_prompt("Acme", None, web)
    assert "CHECKS THAT RETURNED NO DATA" in prompt
    assert "pa11y" in prompt and "html_validate" in prompt
    assert "lighthouse," not in prompt.split("CHECKS THAT RETURNED NO DATA")[1], (
        "a signal that succeeded must not be listed as missing"
    )


def test_prompt_has_no_coverage_warning_when_everything_ran():
    web = _StubWeb(page_speed_score=55, seo_score=60, signal_status={"lighthouse": "ok", "pa11y": "ok"})
    assert "CHECKS THAT RETURNED NO DATA" not in AIAuditor._build_prompt("Acme", None, web)


# ---------------------------------------------------------------------------
# Google Places pagination
#
# The field mask governs the whole response body, not just per-place fields;
# omitting nextPageToken meant the API never returned one and pagination
# silently stopped after page 1, capping every search at ~20 raw results.
# ---------------------------------------------------------------------------

def test_places_field_mask_requests_the_pagination_token():
    import inspect
    from scrapers.google_maps import GoogleMapsScraper

    source = inspect.getsource(GoogleMapsScraper._scrape_via_api)
    assert "nextPageToken" in source.split("X-Goog-FieldMask")[1].split("\n")[0], (
        "nextPageToken must be inside the field mask itself, or the API "
        "never returns a token and pagination breaks after one page"
    )


# ---------------------------------------------------------------------------
# /api/search freezing every other concurrent request, reported live as a
# 502 on a search that happened while an Autopilot audit was still running
#
# _scrape_via_api is a plain synchronous method (a real time.sleep between
# pages plus blocking httpx.Client().post() calls) — every other network
# call in this codebase's async routes is either genuinely async or wrapped
# in asyncio.to_thread specifically so it can't block the shared event loop.
# scrape_google_maps looked async (it's an `async def`) but was calling
# _scrape_via_api directly, so the whole process froze — no other request,
# including an in-progress audit's own awaits or the frontend's 5s
# /api/costs poll, could be served until it returned.
# ---------------------------------------------------------------------------

def test_scrape_google_maps_wraps_the_blocking_call_in_a_thread():
    import inspect
    from scrapers.google_maps import GoogleMapsScraper

    source = inspect.getsource(GoogleMapsScraper.scrape_google_maps)
    assert "asyncio.to_thread" in source and "_scrape_via_api" in source, (
        "_scrape_via_api does real time.sleep()/blocking httpx calls — "
        "calling it directly from this async method freezes every other "
        "concurrent request on the process for its whole duration"
    )


@pytest.mark.asyncio
async def test_scrape_google_maps_does_not_freeze_the_event_loop(monkeypatch):
    """
    Behavioural version of the guard above: actually run scrape_google_maps
    with a slow synchronous stand-in for _scrape_via_api and confirm a
    concurrently-running coroutine still gets to make progress, rather than
    only resuming once the scrape finishes.
    """
    import asyncio
    import time as time_module

    from scrapers.google_maps import GoogleMapsScraper

    scraper = GoogleMapsScraper()
    scraper.api_key = "fake-key-for-this-test"

    def _slow_blocking_scrape(niche, city, limit):
        time_module.sleep(0.3)
        return []

    monkeypatch.setattr(scraper, "_scrape_via_api", _slow_blocking_scrape)

    ticks = 0

    async def _tick_counter():
        nonlocal ticks
        for _ in range(20):
            await asyncio.sleep(0.02)
            ticks += 1

    await asyncio.gather(
        scraper.scrape_google_maps("gym", "Mumbai", limit=5),
        _tick_counter(),
    )

    # A frozen event loop would starve the counter until the blocking call
    # returned, so it would land at (or very near) 0 real ticks logged
    # during that window. asyncio.to_thread keeps the loop free to run it.
    assert ticks > 5, (
        f"only {ticks} ticks progressed while the scrape ran — the event "
        "loop looks frozen"
    )


# ---------------------------------------------------------------------------
# Daily send cap
#
# DAILY_EMAIL_LIMIT was only checked in main.py's batch loop, which never
# sends (it drafts — its "emailed" branch is unreachable), so it constrained
# nothing on the path emails actually leave by.
# ---------------------------------------------------------------------------

def test_daily_cap_is_enforced_on_the_real_send_path():
    import inspect
    import app

    source = inspect.getsource(app.send_email)
    assert "count_emails_sent_today" in source, (
        "/api/send must check real send volume — it is the only path that "
        "actually sends, so the warm-up volume cap is meaningless elsewhere"
    )
    assert "429" in source


def test_daily_cap_returns_429_not_an_opaque_500(monkeypatch):
    """
    Behavioural, not textual: HTTPException subclasses Exception, so a bare
    `except Exception` that re-raises as 500 silently rebrands deliberate
    status codes. That had already happened to the suppression-list 400,
    which a changelog entry claimed was returning 400 while it actually
    returned 500.
    """
    from fastapi.testclient import TestClient
    import app as app_module
    import config

    monkeypatch.setattr(config, "DAILY_EMAIL_LIMIT", 5)
    monkeypatch.setattr(app_module.config, "DAILY_EMAIL_LIMIT", 5)
    monkeypatch.setattr(app_module.db, "count_emails_sent_today", lambda: 5)
    # Auth/rate-limit are separate concerns; keep this test focused.
    monkeypatch.setattr(app_module.config, "API_KEY", None)

    client = TestClient(app_module.app, raise_server_exceptions=False)
    res = client.post("/api/send", json={
        "email": "someone@example.com",
        "subject": "s",
        "body": "b",
        "company": "Acme",
        "website": "https://example.com",
    })

    assert res.status_code == 429, (
        f"expected 429 for the daily cap, got {res.status_code} — a generic "
        "exception handler is likely swallowing it into a 500"
    )
    assert "Daily sending limit" in res.json().get("detail", "")


def test_send_below_the_cap_is_not_blocked(monkeypatch):
    from fastapi.testclient import TestClient
    import app as app_module

    monkeypatch.setattr(app_module.config, "DAILY_EMAIL_LIMIT", 50)
    monkeypatch.setattr(app_module.db, "count_emails_sent_today", lambda: 3)
    monkeypatch.setattr(app_module.config, "API_KEY", None)
    monkeypatch.setattr(app_module.ses, "send_email", lambda *a, **k: "<msg-id@test>")
    monkeypatch.setattr(app_module.db, "log_cost", lambda *a, **k: None)
    monkeypatch.setattr(app_module.db, "log_email", lambda *a, **k: None)
    monkeypatch.setattr(app_module.db, "delete_draft_by_website", lambda *a, **k: None)
    monkeypatch.setattr(app_module.db, "get_draft_by_website", lambda *a, **k: None)

    client = TestClient(app_module.app, raise_server_exceptions=False)
    res = client.post("/api/send", json={
        "email": "someone@example.com",
        "subject": "s",
        "body": "b",
        "company": "Acme",
        "website": "https://example.com",
    })
    assert res.status_code == 200, res.text


def test_audit_proceeds_when_playwright_cannot_access_the_site(monkeypatch):
    """
    If Playwright can't render the page, the audit should proceed with httpx
    rather than erroring out completely.
    """
    from fastapi.testclient import TestClient
    import app as app_module

    async def _fake_generate_audit_screenshot(*a, **k):
        return (None, None, None)

    monkeypatch.setattr(app_module.config, "API_KEY", None)
    monkeypatch.setattr(app_module.ses, "check_quota", lambda: {"Max24HourSend": 100, "SentLast24Hours": 0})
    monkeypatch.setattr(app_module, "generate_audit_screenshot", _fake_generate_audit_screenshot)
    monkeypatch.setattr(app_module.db, "log_cost", lambda *a, **k: None)

    client = TestClient(app_module.app, raise_server_exceptions=False)
    res = client.post("/api/audit", json={"company": "Acme", "website": "https://example.com"})

    assert res.status_code == 200, res.text
    body = res.json()
    assert "error" not in body  # It should not fail
    assert "flaws" in body  # It should draft something with httpx fallback


# ---------------------------------------------------------------------------
# async_mode (added 2026-09-07). Railway's edge times out the underlying
# HTTP request well before a real ~2 minute audit finishes — live-confirmed
# via the deploy log (the audit was still cleanly progressing at 65s+ in, no
# backend error, no crash/restart; the connection was cut in front of the
# app). With async_mode: true, /api/audit returns {"started": true} almost
# immediately and does the real work in a detached task; the frontend polls
# /api/audit/result for the outcome. Default False, so every existing
# caller (main.py, every other test in this file) is unaffected.
# ---------------------------------------------------------------------------

def _mock_audit_pipeline(monkeypatch, app_module):
    """Fast, deterministic stand-ins for every step _audit_lead_impl calls."""
    async def _fake_generate_audit_screenshot(*a, **k):
        return (None, None, None)

    class _FakeWebData:
        page_speed_score = 50
        seo_score = 50
        instagram_url = None
        technologies = []
        has_booking_widget = False
        signal_status = {}

    async def _fake_audit_website(*a, **k):
        return _FakeWebData()

    monkeypatch.setattr(app_module.config, "API_KEY", None)
    monkeypatch.setattr(app_module.config, "MIN_BUDGET_TIER", "")
    monkeypatch.setattr(app_module, "validate_public_url", lambda *a, **k: None)
    monkeypatch.setattr(app_module.ses, "check_quota", lambda: {"Max24HourSend": 100, "SentLast24Hours": 0})
    monkeypatch.setattr(app_module, "generate_audit_screenshot", _fake_generate_audit_screenshot)
    monkeypatch.setattr(app_module.web_scraper, "audit_website", _fake_audit_website)
    monkeypatch.setattr(app_module.auditor, "analyze_lead", lambda *a, **k: {
        "flaws": [], "overall_score": 50, "email_subject": "s", "opening_line": "o",
    })
    monkeypatch.setattr(app_module.decision_maker, "find_decision_maker", lambda *a, **k: {
        "name": "Team", "email": "owner@example.com", "is_guess": False, "domain_accepts_mail": True, "cost": 0.0,
    })
    monkeypatch.setattr(app_module.db, "log_cost", lambda *a, **k: None)
    monkeypatch.setattr(app_module.db, "log_draft", lambda *a, **k: None)
    monkeypatch.setattr(app_module.sheets, "find_row_by_website", lambda *a, **k: None)


def test_async_mode_returns_started_immediately(monkeypatch):
    from fastapi.testclient import TestClient
    import app as app_module

    _mock_audit_pipeline(monkeypatch, app_module)
    client = TestClient(app_module.app, raise_server_exceptions=False)

    res = client.post("/api/audit", json={
        "company": "Acme", "website": "https://async-mode-example.com", "async_mode": True,
    })

    assert res.status_code == 200, res.text
    assert res.json() == {"started": True}


def test_async_mode_result_becomes_ready_and_matches_the_sync_shape(monkeypatch):
    import time
    from fastapi.testclient import TestClient
    import app as app_module

    _mock_audit_pipeline(monkeypatch, app_module)
    website = "https://async-mode-result-example.com"

    # A persistent event loop (the `with` block's portal) is required for
    # the detached asyncio.create_task background audit to actually get
    # turns to run between polls — a fresh TestClient call per request would
    # tear the loop down and abandon the task before it ever progressed.
    with TestClient(app_module.app, raise_server_exceptions=False) as client:
        client.post("/api/audit", json={"company": "Acme", "website": website, "async_mode": True})

        result = None
        for _ in range(50):
            r = client.get("/api/audit/result", params={"website": website})
            if r.json().get("ready"):
                result = r.json()
                break
            time.sleep(0.05)

    assert result is not None, "background audit never became ready"
    assert "error" not in result
    assert result["flaws"] == []
    assert result["overall_score"] == 50


def test_async_mode_result_is_popped_after_one_read(monkeypatch):
    import time
    from fastapi.testclient import TestClient
    import app as app_module

    _mock_audit_pipeline(monkeypatch, app_module)
    website = "https://async-mode-pop-example.com"

    with TestClient(app_module.app, raise_server_exceptions=False) as client:
        client.post("/api/audit", json={"company": "Acme", "website": website, "async_mode": True})

        for _ in range(50):
            if client.get("/api/audit/result", params={"website": website}).json().get("ready"):
                break
            time.sleep(0.05)

        second = client.get("/api/audit/result", params={"website": website})

    assert second.json() == {"ready": False}, "a one-shot handoff must not replay the same result forever"


# ---------------------------------------------------------------------------
# /api/audit/cancel (added 2026-09-09, "add an option of cancelling the
# ongoing audit"). task.cancel() raises CancelledError at whatever await
# point the audit is sitting at; _run_and_store's handler stores a clean
# {"cancelled": True} result instead of leaving /api/audit/result with
# nothing to ever return.
# ---------------------------------------------------------------------------

def test_cancel_returns_false_when_nothing_is_running(monkeypatch):
    from fastapi.testclient import TestClient
    import app as app_module

    monkeypatch.setattr(app_module.config, "API_KEY", None)
    client = TestClient(app_module.app, raise_server_exceptions=False)

    res = client.post("/api/audit/cancel", params={"website": "https://nothing-running-here.com"})

    assert res.status_code == 200, res.text
    assert res.json() == {"cancelled": False}


def test_cancel_stops_an_in_flight_audit_and_result_reports_cancelled(monkeypatch):
    import asyncio
    import time
    from fastapi.testclient import TestClient
    import app as app_module

    _mock_audit_pipeline(monkeypatch, app_module)
    website = "https://cancel-mid-audit-example.com"

    class _FakeWebData:
        page_speed_score = 50
        seo_score = 50
        instagram_url = None
        technologies = []
        has_booking_widget = False
        signal_status = {}

    # A real await point with enough of a window for the test to land
    # /api/audit/cancel while the task is still sitting inside it — the
    # other mocked pipeline steps return instantly, which would make
    # cancellation a race the test can't reliably win.
    async def _slow_audit_website(*a, **k):
        await asyncio.sleep(2)
        return _FakeWebData()

    monkeypatch.setattr(app_module.web_scraper, "audit_website", _slow_audit_website)

    # /api/audit's rate-limit bucket is keyed by X-API-Key and shared for the
    # whole test process (see rate_limit()'s own docstring) — a distinct key
    # here keeps this test's extra /api/audit call off the "anonymous" bucket
    # other tests in this file already budget against.
    headers = {"X-API-Key": "test-cancel-mid-audit"}

    with TestClient(app_module.app, raise_server_exceptions=False) as client:
        client.post("/api/audit", json={"company": "Acme", "website": website, "async_mode": True}, headers=headers)

        # Wait until the audit is actually running (past stage 1, inside the
        # slow audit_website call) before cancelling — cancelling before it
        # starts would just be testing the "nothing running" path again.
        for _ in range(50):
            if client.get("/api/audit/progress", params={"website": website}).json().get("running"):
                break
            time.sleep(0.02)

        cancel_res = client.post("/api/audit/cancel", params={"website": website})
        assert cancel_res.json() == {"cancelled": True}

        result = None
        for _ in range(100):
            r = client.get("/api/audit/result", params={"website": website})
            if r.json().get("ready"):
                result = r.json()
                break
            time.sleep(0.02)

    assert result == {"ready": True, "cancelled": True}
    # The `finally` in _audit_lead_impl must have cleared progress too —
    # otherwise the frontend's poll loop would see "still running" forever
    # despite a result already sitting in /api/audit/result.
    assert not client.get("/api/audit/progress", params={"website": website}).json()["running"]


def test_cancelling_a_website_with_no_task_returns_false_even_after_normal_completion(monkeypatch):
    """
    _audit_tasks entries remove themselves via a done-callback once the task
    finishes — cancelling the same website again afterwards must not find a
    stale, already-finished task and report a bogus {"cancelled": True}.
    """
    import time
    from fastapi.testclient import TestClient
    import app as app_module

    _mock_audit_pipeline(monkeypatch, app_module)
    website = "https://cancel-after-completion-example.com"
    # See the previous test's comment — own bucket, off the shared one.
    headers = {"X-API-Key": "test-cancel-after-completion"}

    with TestClient(app_module.app, raise_server_exceptions=False) as client:
        client.post("/api/audit", json={"company": "Acme", "website": website, "async_mode": True}, headers=headers)
        for _ in range(50):
            if client.get("/api/audit/result", params={"website": website}).json().get("ready"):
                break
            time.sleep(0.05)

        res = client.post("/api/audit/cancel", params={"website": website})

    assert res.json() == {"cancelled": False}


def test_cancel_route_is_wired_and_gated_the_same_as_every_other_get(monkeypatch):
    """Source-inspection check, same convention as test_sent_websites.py's equivalent."""
    import inspect
    import app as app_module

    src = inspect.getsource(app_module.cancel_audit)
    assert "require_api_key" in src
    assert "rate_limit" in src
    assert "_audit_tasks" in src


def test_async_mode_with_no_website_falls_through_to_the_sync_path(monkeypatch):
    from fastapi.testclient import TestClient
    import app as app_module

    _mock_audit_pipeline(monkeypatch, app_module)
    client = TestClient(app_module.app, raise_server_exceptions=False)

    res = client.post("/api/audit", json={"company": "Acme", "website": "", "async_mode": True})

    assert res.status_code == 200, res.text
    assert "started" not in res.json(), "no website means nothing to key the background result on — must run synchronously"


def test_audit_result_reports_not_ready_when_nothing_was_ever_started(monkeypatch):
    from fastapi.testclient import TestClient
    import app as app_module

    monkeypatch.setattr(app_module.config, "API_KEY", None)
    client = TestClient(app_module.app, raise_server_exceptions=False)
    res = client.get("/api/audit/result", params={"website": "https://never-audited-example.com"})

    assert res.json() == {"ready": False}


def test_count_emails_sent_today_reads_the_send_log():
    import inspect
    from storage import db

    source = inspect.getsource(db.count_emails_sent_today)
    assert "email_history" in source, "the cap must count real sends, not an in-process counter"
    assert "-1 day" in source


# ---------------------------------------------------------------------------
# "Website is unreachable" false positive
#
# audit_website() used to treat html=None (Playwright failed both retries)
# as proof the site was down, and that exact wording became the AI's "Your
# website isn't loading" email. Live-verified on a real lead
# (brandyaar.com): Playwright failed the run that produced the email, but
# the site loaded perfectly on the very next attempt and responded
# normally to a plain HTTP request the whole time — bot-detection/a JS
# loading screen defeated the headless browser, the site was never down.
# A cheap httpx probe now distinguishes "our tool failed" from "the site is
# actually unreachable" before either wording is allowed into a flaw.
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class _FakeClientReachable:
    async def get(self, *args, **kwargs):
        return _FakeResponse(200)


class _FakeClientGenuinelyDown:
    async def get(self, *args, **kwargs):
        raise Exception("connection refused")


async def test_playwright_failure_does_not_claim_site_is_down_when_httpx_succeeds():
    scraper = WebsiteScraper()
    scraper.client = _FakeClientReachable()
    data = await scraper.audit_website("https://example.com", html=None, extra_audit_data=None)

    assert data.reachable is False  # we still couldn't run the real audit
    description = data.flaws[0].description.lower()
    assert "down" not in description
    assert "unreachable" not in description
    assert "reachable" in description  # states plainly that it IS reachable


async def test_playwright_failure_does_not_imply_an_seo_crawling_problem():
    """
    Live-verified 2026-08-07, same day as the fix above: the softened
    wording still got inflated by the AI into "this barrier makes it harder
    for search engines to crawl and index your content" — an unsupported
    claim, since a headless-browser block proves nothing about whether
    Googlebot (frequently allowlisted separately) is also blocked.
    """
    scraper = WebsiteScraper()
    scraper.client = _FakeClientReachable()
    data = await scraper.audit_website("https://example.com", html=None, extra_audit_data=None)

    flaw = data.flaws[0]
    description = flaw.description.lower()
    assert "search engine" not in description or "do not" in description or "no evidence" in description
    assert "crawl" not in description or "not claim" in description or "no evidence" in description
    # An audit-tooling gap, not a confirmed site defect — must not carry
    # critical-flaw weight (see compute_score, which this feeds directly).
    assert flaw.severity == "low"


async def test_playwright_failure_keeps_unreachable_wording_when_httpx_also_fails():
    scraper = WebsiteScraper()
    scraper.client = _FakeClientGenuinelyDown()
    data = await scraper.audit_website("https://example.com", html=None, extra_audit_data=None)

    flaw = data.flaws[0]
    assert "unreachable" in flaw.description.lower()
    assert flaw.severity == "critical"  # a genuine, verified outage is a real critical flaw


# ---------------------------------------------------------------------------
# The send gate (added 2026-08-09). The five accuracy checks in
# analyzer/ai_audit.py have flagged suspect copy since 2026-08-07 and the
# Drafts UI has shown it — but nothing stopped a flagged draft going out, so
# the whole safety net rested on a human happening to read a red banner.
# A draft's audit data is also frozen at generation time while the draft sits
# in the inbox indefinitely, so old findings can be quoted as current fact.
# ---------------------------------------------------------------------------

def _send_client(monkeypatch, draft):
    """A TestClient whose /api/send sees exactly `draft` (or None)."""
    from fastapi.testclient import TestClient
    import app as app_module

    monkeypatch.setattr(app_module.config, "API_KEY", None)
    monkeypatch.setattr(app_module.db, "count_emails_sent_today", lambda: 0)
    monkeypatch.setattr(app_module.db, "get_draft_by_website", lambda website: draft)
    # If the gate lets the request through, stop before really sending.
    monkeypatch.setattr(app_module.ses, "send_email", lambda *a, **k: "<msg-id@example.com>")
    monkeypatch.setattr(app_module.db, "log_cost", lambda *a, **k: None)
    monkeypatch.setattr(app_module.db, "log_email", lambda *a, **k: None)
    monkeypatch.setattr(app_module.db, "delete_draft_by_website", lambda *a, **k: None)
    monkeypatch.setattr(app_module.sheets, "find_row_by_website", lambda *a, **k: None)

    return TestClient(app_module.app, raise_server_exceptions=False)


_SEND_PAYLOAD = {
    "email": "owner@example.com",
    "subject": "Quick note",
    "body": "Hi there",
    "company": "Acme",
    "website": "https://example.com",
}


def test_flagged_draft_is_not_sent_without_acknowledgement(monkeypatch):
    draft = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "review_warnings": ["Cites number(s) ['847'] not found anywhere in the source data"],
    }
    client = _send_client(monkeypatch, draft)

    res = client.post("/api/send", json=_SEND_PAYLOAD)

    assert res.status_code == 409, res.text
    detail = res.json()["detail"]
    assert any("847" in w for w in detail["warnings"])


def test_flagged_draft_sends_once_acknowledged(monkeypatch):
    draft = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "review_warnings": ["Grounding check flagged 1 claim(s) as possibly unsupported"],
    }
    client = _send_client(monkeypatch, draft)

    res = client.post("/api/send", json={**_SEND_PAYLOAD, "acknowledge_warnings": True})

    assert res.status_code == 200, res.text
    assert res.json()["status"] == "success"


def test_clean_draft_sends_with_no_extra_step(monkeypatch):
    draft = {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "review_warnings": []}
    client = _send_client(monkeypatch, draft)

    res = client.post("/api/send", json=_SEND_PAYLOAD)

    assert res.status_code == 200, res.text


def test_stale_draft_is_not_sent_without_acknowledgement(monkeypatch):
    """A draft old enough that its audit data may no longer describe the site."""
    import app as app_module

    old = datetime.now() - timedelta(days=app_module.config.DRAFT_STALE_DAYS + 3)
    draft = {"timestamp": old.strftime("%Y-%m-%d %H:%M:%S"), "review_warnings": []}
    client = _send_client(monkeypatch, draft)

    res = client.post("/api/send", json=_SEND_PAYLOAD)

    assert res.status_code == 409, res.text
    assert any("days ago" in w for w in res.json()["detail"]["warnings"])


def test_an_unparseable_draft_timestamp_does_not_block_a_clean_send(monkeypatch):
    """A bad timestamp is a data problem, not a reason to refuse a clean draft."""
    draft = {"timestamp": "not-a-timestamp", "review_warnings": []}
    client = _send_client(monkeypatch, draft)

    res = client.post("/api/send", json=_SEND_PAYLOAD)

    assert res.status_code == 200, res.text


def test_send_with_no_matching_draft_row_is_unaffected(monkeypatch):
    """/api/send is also called directly from the audit view, with no draft saved."""
    client = _send_client(monkeypatch, None)

    res = client.post("/api/send", json=_SEND_PAYLOAD)

    assert res.status_code == 200, res.text


def test_acknowledgement_defaults_to_false_so_the_gate_fails_closed():
    """A caller unaware of the gate must not bypass it by omitting the field."""
    from app import SendRequest

    req = SendRequest(email="a@b.com", subject="s", body="b", company="c", website="w")
    assert req.acknowledge_warnings is False
