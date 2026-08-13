"""
Budget-fit signal — a rough, free estimate of whether a lead can plausibly
afford this service, for the OPERATOR to see, never the lead (added
2026-08-10, on the founder's own question: "how do we know they'll spend?").

His original idea — public P&L for listed companies, PR/news/LinkedIn
mentions otherwise — doesn't fit this tool's actual lead population. The
leads this pipeline targets (see searchOptions.js's NICHES: yoga studios,
dental clinics, salons, gyms) are essentially never publicly listed, and a
PR/news search costs real money per lead while returning almost nothing for
a neighbourhood business that has never been written about. Querying an API
per lead to reliably learn "no data" is a bad trade.

One real, free, official signal DOES exist for the subset of leads that are
registered as a company rather than a sole proprietorship: India's MCA
(Ministry of Corporate Affairs) publishes paid-up/authorized capital as open
government data via data.gov.in — see analyzer/mca_lookup.py. Added
2026-08-10 on explicit request despite the coverage/risk caveats documented
there (bulk-registry, exact-match-only, low hit rate for this lead
population); wired in here as an optional `mca_match` param so the rest of
this module works identically whether or not that lookup ever fires.

Every signal here instead comes from data this pipeline ALREADY scrapes for
other reasons, so this costs nothing extra — no new request, no new API,
no new per-lead spend:

  - Google review count/rating (scrapers/google_maps.py, already fetched
    to find the lead in the first place) — the closest free proxy this
    project has to customer volume for a local service business.
  - Whether the site already runs paid booking/scheduling tooling
    (WebsiteData.has_booking_widget) or ad/marketing-automation tech
    (WebsiteData.technologies, via Wappalyzer) — direct evidence the
    business already spends money on customer acquisition or operations,
    which is stronger evidence than anything inferred from press coverage.
  - Instagram follower count (InstagramData.followers, when available) —
    a second free scale proxy independent of Google.

Empirically sparse by design, not by bug: sampling three real leads this
session found Google Analytics/Tag Manager on nearly everything (near-
universal, so NOT scored — it discriminates nothing) but no ad pixel, CRM,
or payment processor on any of them. That is expected for this lead
population — small local businesses rarely run dedicated ad tracking — so
_AD_TECH_KEYWORDS firing at all is treated as a real positive precisely
because it's rare, not filtered out for being rare.

This is a PRIORITISATION heuristic for a human, not a fact about the lead
and not something the lead ever sees — it lives only in /api/audit's
response and the dashboard UI. Nothing in analyzer/ai_audit.py or
emailer/base_sender.py reads it, so it cannot leak into drafted copy.
"""

# Deliberately excludes has_click_to_call/has_whatsapp_link — sampling real
# leads showed both present on nearly every small business site, so neither
# discriminates between "can afford this" and "can't"; scoring them would
# just add noise. has_booking_widget IS scored: a paid scheduling tool
# (unlike a bare tel:/wa.me link) is a real recurring software cost the
# business has already chosen to take on.

# Names as they appear in Wappalyzer's output (Title Case product names).
# Kept short and high-confidence rather than exhaustive — a false negative
# here just means one fewer point on a 5ish-point scale, but a false
# positive (matching a name that isn't really what it looks like) would
# mislead the one thing this score exists to inform.
_AD_TECH_KEYWORDS = (
    "google ads", "meta pixel", "facebook pixel", "tiktok pixel",
    "linkedin insight", "hubspot", "salesforce", "mailchimp",
    "klaviyo", "zoho crm",
)
_PAYMENT_TECH_KEYWORDS = (
    "razorpay", "stripe", "payu", "instamojo", "paytm", "woocommerce",
    "shopify", "cart functionality",
)

# Below this many reviews, a rating is one or two friends/family and not a
# real signal of customer volume.
_MIN_REVIEWS_FOR_RATING_SIGNAL = 10
_HIGH_REVIEW_COUNT = 200
_MODERATE_REVIEW_COUNT = 50
_HIGH_FOLLOWER_COUNT = 5000

_TIER_LABELS = {
    "established": "Likely has budget — established customer base and/or already spends on marketing/tooling.",
    "growing": "Some signal of scale — worth qualifying on the call rather than assuming either way.",
    "unclear": "Not enough signal to judge — low/no review data and no marketing or payment tooling detected.",
}


_INACTIVE_MCA_STATUSES = (
    "strike off", "struck off", "dissolved", "amalgamated",
    "under liquidation", "converted", "dormant",
)


def _format_inr(value) -> str | None:
    try:
        return f"₹{int(float(value)):,}"
    except (TypeError, ValueError):
        return None


def estimate_budget_fit(
    *,
    rating: float | str | None = None,
    reviews_count: int | None = None,
    technologies: list[str] | None = None,
    has_booking_widget: bool = False,
    ig_followers: int | None = None,
    mca_match: dict | None = None,
) -> dict:
    """
    A rough {"tier": "established"|"growing"|"unclear", "signals": [...],
    "label": str} estimate of whether this lead can plausibly afford the
    service — built entirely from data already in hand, at zero extra cost.

    Never a confident claim: "unclear" is the honest, common answer for a
    brand-new business with few reviews and no detectable tooling, and
    that's not the same as "can't afford it" — just "we don't know."
    """
    points = 0
    signals: list[str] = []

    try:
        review_count = int(reviews_count) if reviews_count not in (None, "") else 0
    except (TypeError, ValueError):
        review_count = 0

    if review_count >= _HIGH_REVIEW_COUNT:
        points += 2
        signals.append(f"{review_count} Google reviews — high customer volume")
    elif review_count >= _MODERATE_REVIEW_COUNT:
        points += 1
        signals.append(f"{review_count} Google reviews — moderate customer volume")

    try:
        rating_value = float(rating) if rating not in (None, "", "N/A") else None
    except (TypeError, ValueError):
        rating_value = None
    if rating_value is not None and rating_value >= 4.5 and review_count >= _MIN_REVIEWS_FOR_RATING_SIGNAL:
        points += 1
        signals.append(f"{rating_value:g}★ rating across a meaningful review count")

    tech_lower = {t.lower() for t in (technologies or [])}
    if any(any(kw in t for kw in _AD_TECH_KEYWORDS) for t in tech_lower):
        points += 1
        signals.append("Already running paid ad-tracking or marketing-automation tooling")
    if any(any(kw in t for kw in _PAYMENT_TECH_KEYWORDS) for t in tech_lower):
        points += 1
        signals.append("Already running paid payment/commerce tooling")

    if has_booking_widget:
        points += 1
        signals.append("Already pays for online booking/scheduling software")

    try:
        followers = int(ig_followers) if ig_followers not in (None, "") else 0
    except (TypeError, ValueError):
        followers = 0
    if followers >= _HIGH_FOLLOWER_COUNT:
        points += 1
        signals.append(f"{followers:,} Instagram followers — meaningful reach")

    # A confident MCA match (analyzer/mca_lookup.py) is real filed financial
    # data, not a proxy — worth more than every other signal here combined,
    # so it's scored high enough to guarantee "established" on its own. A
    # dissolved/struck-off company is NOT scored positively (that status
    # says the opposite of "can spend on this"), but also not scored
    # negatively — stale MCA data plus a business that simply re-registered
    # under a new entity is a real possibility, and asserting a negative
    # from it would be a worse mistake than staying silent.
    if mca_match:
        status = (mca_match.get("status") or "").lower()
        if not any(bad in status for bad in _INACTIVE_MCA_STATUSES):
            points += 3
            capital = _format_inr(mca_match.get("paidup_capital"))
            detail = f" (paid-up capital {capital})" if capital else ""
            signals.append(f"Registered with MCA as {mca_match.get('company_name') or 'a company'}{detail}")
        else:
            signals.append(f"MCA record found but status is '{mca_match.get('status')}' — not treated as a positive signal")

    if points >= 3:
        tier = "established"
    elif points >= 1:
        tier = "growing"
    else:
        tier = "unclear"

    return {"tier": tier, "points": points, "signals": signals, "label": _TIER_LABELS[tier]}
