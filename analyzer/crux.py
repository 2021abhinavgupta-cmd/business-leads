"""
Chrome UX Report (CrUX) — real-user field data for Core Web Vitals.

This is the most authoritative performance signal available for free: actual
measurements from real Chrome users who visited the site, aggregated at the
75th percentile over a rolling 28-day window, published by Google. It beats
both other sources this project has:

  - Lighthouse / PageSpeed lab scores are a *simulation* on throttled CPU and
    network. They routinely report 5-10x worse than a real visit, which is
    how an audit email once claimed a site's hero took 17.5s to render when a
    real visitor saw ~2s.
  - analyzer/visuals.py's PerformanceObserver capture is a real page load, but
    it is exactly ONE load, from a datacentre, on a fast connection, with no
    cache — a sample size of one from an unrepresentative client.

CrUX is neither simulated nor a single sample, so it is preferred over both
when available (see scrapers/website.py's per-metric priority chain).

Two ways in, because they fail in different situations:

  1. `fetch_crux_vitals()` — the dedicated CrUX API. Cheap (~1s, no lab audit)
     and works regardless of whether Lighthouse succeeded. Requires the
     "Chrome UX Report API" to be enabled on the Google Cloud project that
     owns PAGESPEED_KEY; returns 403 until then, which is treated as "no
     data" rather than an error.
  2. `extract_crux_from_pagespeed()` — pulls the same numbers out of the
     `loadingExperience` block that the PageSpeed API already returns and
     this project currently discards. Free (the call is already being made)
     but only available on the code path where PageSpeed actually runs,
     i.e. when the Lighthouse CLI was unavailable.

Not every URL has CrUX data: Google only publishes it for origins/pages with
enough traffic (thousands of visits/month), so small-business sites — a large
share of this project's leads — will often legitimately have none. Absence is
normal and must degrade to the next source, never to a zero.
"""

import httpx

import config

CRUX_URL = "https://chromeuxreport.googleapis.com/v1/records:queryRecord"

# CrUX metric name -> the key names used throughout this project.
_METRIC_MAP = {
    "largest_contentful_paint": "lcp_ms",
    "cumulative_layout_shift": "cls",
    "interaction_to_next_paint": "inp_ms",
    "experimental_time_to_first_byte": "ttfb_ms",
}

# The same metrics as they're named in PageSpeed's loadingExperience block,
# which uses SHOUTING_CASE and a different spelling for the same things.
_PAGESPEED_METRIC_MAP = {
    "LARGEST_CONTENTFUL_PAINT_MS": "lcp_ms",
    "CUMULATIVE_LAYOUT_SHIFT_SCORE": "cls",
    "INTERACTION_TO_NEXT_PAINT": "inp_ms",
    "EXPERIMENTAL_TIME_TO_FIRST_BYTE": "ttfb_ms",
}


# CLS arrives in two genuinely different shapes depending on which endpoint
# produced it, and getting this wrong is silent: the value either vanishes or
# lands 100x off, and 0.01 vs 1.0 is the difference between "fine" and a
# critical flaw. Live-verified 2026-08-03 against the same site:
#   dedicated CrUX API        -> "0.01"  (string, already the decimal form)
#   PageSpeed loadingExperience ->  1     (integer, multiplied by 100)
# Every threshold in this codebase, and Google's own "good < 0.10" guidance,
# uses the decimal form, so both are converted to that here rather than
# leaving the flaw-building code to know where a number came from.

def _cls_from_crux_api(raw) -> float | None:
    """Dedicated CrUX API: a decimal, but delivered as a string."""
    try:
        return round(float(raw), 3)
    except (TypeError, ValueError):
        return None


def _cls_from_pagespeed(raw) -> float | None:
    """PageSpeed loadingExperience: an integer at 100x the real value."""
    if not isinstance(raw, (int, float)):
        return None
    return round(raw / 100, 3)


async def fetch_crux_vitals(url: str, form_factor: str = "PHONE") -> dict:
    """
    Query the dedicated CrUX API for real-user field vitals.

    Returns a dict with any of lcp_ms/cls/inp_ms/ttfb_ms that Google has data
    for, or {} when the URL has insufficient traffic (404), the API isn't
    enabled on the project (403), or anything else goes wrong. Degrading to
    {} is deliberate — callers fall through to the next-best source.
    """
    if not config.PAGESPEED_KEY:
        return {}

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                CRUX_URL,
                params={"key": config.PAGESPEED_KEY},
                json={"url": url, "formFactor": form_factor},
            )

        if response.status_code == 404:
            # Genuinely normal: Google publishes CrUX only for URLs with
            # enough real traffic, and plenty of small-business leads won't
            # clear that bar.
            print(f"[CrUX] No field data published for {url} (not enough real-user traffic) — falling back to measured/lab vitals.")
            return {}

        if response.status_code == 403:
            # Two distinct causes that need different fixes, and guessing
            # wrong sends you to the wrong console page:
            #   SERVICE_DISABLED      -> enable the API on the GCP project
            #   API_KEY_SERVICE_BLOCKED -> the API is enabled, but the key's
            #                              own "API restrictions" allowlist
            #                              doesn't include this service
            # Surface Google's actual reason rather than asserting one.
            error = response.json().get("error", {})
            reason = ""
            for detail in error.get("details", []):
                if detail.get("reason"):
                    reason = detail["reason"]
                    break

            if reason == "API_KEY_SERVICE_BLOCKED":
                hint = "the API key's own API-restrictions allowlist doesn't include Chrome UX Report API — add it under APIs & Services > Credentials > (your key) > API restrictions"
            elif reason == "SERVICE_DISABLED":
                hint = "the Chrome UX Report API isn't enabled on this project — enable it under APIs & Services > Library"
            else:
                hint = error.get("message", "permission denied")

            print(f"[CrUX] Field data unavailable ({reason or '403'}): {hint}. Falling back to measured/lab vitals.")
            return {}

        response.raise_for_status()
        metrics = response.json().get("record", {}).get("metrics", {})
        return _metrics_to_vitals(metrics)

    except Exception as e:
        print(f"[CrUX] Lookup failed for {url}: {e} — falling back to measured/lab vitals.")
        return {}


def _metrics_to_vitals(metrics: dict) -> dict:
    """Map a CrUX API `metrics` block to this project's vitals keys."""
    vitals: dict = {}
    for crux_name, our_name in _METRIC_MAP.items():
        p75 = (metrics.get(crux_name) or {}).get("percentiles", {}).get("p75")
        if p75 is None:
            continue

        if our_name == "cls":
            value = _cls_from_crux_api(p75)
        else:
            try:
                value = round(float(p75))
            except (TypeError, ValueError):
                value = None

        # Drop rather than store a null: callers test `is not None` to decide
        # whether to fall through to the next-best source, so a None left in
        # the dict would masquerade as "CrUX answered" and block the fallback.
        if value is not None:
            vitals[our_name] = value

    if vitals:
        print(f"[CrUX] Real-user field data: {vitals}")
    return vitals


def extract_crux_from_pagespeed(data: dict) -> dict:
    """
    Pull CrUX field data out of a PageSpeed Insights response.

    PageSpeed already returns this in `loadingExperience` (page-level) and
    `originLoadingExperience` (whole-domain fallback) — the call has been
    made and paid for either way, so this is free real-user data that was
    previously thrown away in favour of the lab numbers alone.
    """
    experience = data.get("loadingExperience") or data.get("originLoadingExperience") or {}
    metrics = experience.get("metrics") or {}

    vitals: dict = {}
    for ps_name, our_name in _PAGESPEED_METRIC_MAP.items():
        percentile = (metrics.get(ps_name) or {}).get("percentile")
        if percentile is None:
            continue

        value = _cls_from_pagespeed(percentile) if our_name == "cls" else round(percentile)
        if value is not None:
            vitals[our_name] = value

    if vitals:
        print(f"[CrUX] Real-user field data (via PageSpeed response): {vitals}")
    return vitals
